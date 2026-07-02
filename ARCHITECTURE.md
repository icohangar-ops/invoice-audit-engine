# Architecture

This document specifies the audit engine's design against the Precoro API's
actual, empirically measured constraints.

## Measured API constraints

These were established by direct probing (July 2026):

| # | Constraint | Evidence / consequence |
|---|---|---|
| 1 | **Route-based rate limit: 1 request/minute per route** | `429`-style JSON body: `{"error":"Too many requests...","RateLimit-Type":"Route based limiter by minute","RateLimit-Limit":1}` with a `RateLimit-Retry-After` UTC timestamp. `/invoices` and `/suppliers` are separate buckets. |
| 2 | **Dual-header auth** | `X-AUTH-TOKEN` + `email` header; the email must be the key owner's. Wrong email ⇒ same `401 Bad credentials` as wrong token — indistinguishable, so config errors must be surfaced clearly at startup. |
| 3 | **User-agent filtering at the edge** | Python's default `urllib` UA gets `403 Forbidden` before auth; curl passes. The client pins a curl-style `User-Agent`. |
| 4 | **List endpoint shape** | `GET /invoices?page=N&perPage=100` returns newest-first by create date, with the supplier embedded but **no line items**. Pagination via `meta.pagination.has_next_page`. |
| 5 | **Detail endpoint keyed by document number** | `GET /invoices/{idn}` (the sequential document number), **not** the internal `id` — using `id` returns 404. Line items arrive as nested dicts keyed by item id, requiring a recursive walk. |
| 6 | **`modifiedSince` filter exists** | Supported on list endpoints for incremental pulls. |

## Design consequences

### 1. Two-tier data model, two-tier rules

Because the list endpoint has headers only (#4) and hydrating details costs one
request-minute *each* (#1, #5), the engine splits:

- **Header tier** — synced for every invoice, cheaply (100/page). Rules that run
  on headers alone: duplicates, entry lag, overdue aging, amount outliers.
- **Item tier** — hydrated selectively. Rules needing line items: rate changes,
  new charge types, credits, tax inconsistency.

A full detail hydration of 700 invoices would take ~12 hours; the engine never
does that. Instead it maintains a **hydration queue** ranked by expected yield:

1. invoices from vendors with existing high-severity header findings,
2. invoices whose total deviates from the vendor median,
3. newest invoices from the highest-spend vendors,
4. everything else, backfilled opportunistically.

At ~60 details/hour, the queue drains the interesting 10% of a year's invoices
in about an hour of wall-clock time, running unattended.

### 2. Resumable, persist-before-advance sync

Every page is upserted and committed **before** the next request is issued.
A crash, timeout, or rate-limit stall loses at most one in-flight page.
Steady-state syncs use `modifiedSince` (#6) from a stored high-water mark
(max `updateDate` seen), so a nightly incremental pull is typically 1–3
requests, not 8+.

```
sync loop:
  page = 1
  while true:
    body = GET /invoices?page&perPage=100&modifiedSince=high_water_mark
    upsert all rows; COMMIT                # durability point
    if body says rate-limited: sleep until RateLimit-Retry-After, retry
    if no next page: break
    sleep 62s                              # stay under the route limiter
  advance high_water_mark
```

### 3. Rate-limit handling is per-route and cooperative

The client serializes calls per route prefix and enforces a 62-second minimum
interval (limit is 60s; 2s of margin absorbs clock skew). On a limit response
it honors `RateLimit-Retry-After` rather than blind exponential backoff —
the server tells us exactly when the bucket refills. Distinct routes
(`/invoices`, `/suppliers`, `/invoices/{idn}`) interleave, so a supplier
refresh can ride along during an invoice sync at no cost.

### 4. Rules are pure; storage is dumb

Rules are pure functions `(invoices, items, config) -> findings` over plain
dicts. No rule touches the network or the database. This makes every rule
unit-testable in milliseconds against in-memory SQLite and keeps the sync,
storage, and analysis layers independently replaceable.

Findings are snapshotted per run (`audit_findings`), replacing the previous
snapshot. Diffing consecutive snapshots to emit only *new* findings (for
Slack/email alerting) is a planned increment that requires no schema change —
findings are already keyed by (rule, vendor, invoice).

### 5. SQLite, deliberately

- Ingest is capped upstream at ~100 rows/minute (#1) — write concurrency is a
  non-problem.
- A decade of invoices for a mid-size plant is tens of thousands of rows —
  volume is a non-problem.
- The read path is one analyst's dashboard, not a fleet of services.

WAL mode gives the dashboard non-blocking reads during sync. If the product
grows to multi-tenant hosting, swap `db.py` for Postgres; the schema is
portable and no rule code changes. Databases oriented at real-time state
sync (e.g. SpacetimeDB) solve a different problem — low-latency multiplayer
mutation — and would trade away mature SQL analytics for nothing this
workload needs.

## Module map

```
auditengine/
  config.py    env-based settings (.env supported), data paths
  precoro.py   rate-limit-aware client: throttle, retry, UA pinning, pagination
  store.py     schema + upserts; offline JSON-page import for air-gapped runs
  rules.py     8 pure rules + AuditConfig thresholds + normalize_number
  db.py        SQLite connection helper (WAL, row factory)
  web.py       FastAPI dashboard: findings table, sync/import actions, CSV export
  cli.py       headless sync / import / run for cron and CI
  ui.py        server-rendered HTML helpers (no JS build step)
```

## Failure modes and mitigations

| Failure | Behavior |
|---|---|
| Rate-limit response mid-sync | Sleep per `Retry-After`, retry same page; nothing lost |
| Network drop mid-sync | Next run resumes from high-water mark; last committed page is durable |
| Bad token or wrong email | Fail fast at first call with an explicit remediation message (both misconfigurations produce the same 401 upstream) |
| Vendor renames an item ("Fuel Adj" → "Fuel Adjustment %") | Baseline resets; surfaced as `new_charge_type` instead of `rate_change` — still flagged, different rule |
| Invoice edited in Precoro after sync | `modifiedSince` re-delivers it; upsert replaces the row and the next rule run re-evaluates |
