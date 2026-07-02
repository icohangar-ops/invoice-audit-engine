# Invoice Audit Engine

Continuous anomaly detection for vendor invoices in [Precoro](https://precoro.com)-based
procure-to-pay. Every invoice a vendor sends is checked against that vendor's own
billing history — the engine catches the slow, quiet ways spend leaks: a unit rate
that creeps up without an amendment, a surcharge line that appears mid-relationship,
a credit with no explanation, a duplicate invoice hiding behind a typo.

Built after a manual audit of a single vendor's 12-month history surfaced ~4% of
spend in questionable charges. This engine runs that audit on every vendor, every day.

## What it detects

| Rule | Severity | What it catches |
|---|---|---|
| `rate_change` | high | Recurring line item billed above its historical baseline price (e.g. a disposal rate that moves $1.00 → $1.50/unit with no contract amendment) |
| `duplicate_invoice_number` | high | Same normalized invoice number appearing twice for one vendor — catches `INV1234` vs `#INV1234` vs `NV1234` typo variants that defeat naive duplicate checks |
| `overdue_unpaid` | high/med | Approved invoices aging past due date — quantifies late-fee exposure before it compounds |
| `new_charge_type` | med | Line-item types (fuel surcharges, shipping, "adjustments") that first appear after the vendor relationship is established |
| `inconsistent_tax` | med | The same item taxed on some invoices and not others |
| `amount_outlier` | med | Invoice totals several multiples above the vendor's median |
| `entry_lag` | med/high | Invoices entered into procurement weeks after their issue date — every intervening close understated cost |
| `unexplained_credit` | low | Negative adjustment lines with no documented reason |

All thresholds are tunable via `AuditConfig` (baseline window, lag tolerance,
outlier multiple, rate-change percentage).

## Quick start

```bash
uv sync
cp .env.example .env        # add PRECORO_TOKEN and PRECORO_EMAIL

# CLI
uv run python -m auditengine.cli sync            # pull invoices (rate-limited)
uv run python -m auditengine.cli import ./pages  # or load exported JSON pages
uv run python -m auditengine.cli run             # re-run rules

# Web dashboard
uv run uvicorn auditengine.web:app --port 8080
```

The dashboard shows findings ranked by severity with dollar amounts, KPI
rollups, and a `findings.csv` export for the AP team.

## Precoro API notes

Authentication requires **two headers**: `X-AUTH-TOKEN` (Configuration →
Integrations → API Key) and `email` — the email of the user who generated the
key. A mismatched email returns the same `401 Bad credentials` as a bad token.

Precoro enforces a **route-based rate limit of ~1 request/minute**. The client
throttles, retries with backoff, and persists every page before requesting the
next, so syncs are resumable and safe to interrupt. A 12-month history
(~700 invoices) syncs in roughly 10–15 minutes. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the sync and rule pipeline are
designed around these constraints.

## Storage

SQLite (`data/audit.db`), deliberately. The workload is small, append-mostly,
and effectively single-writer — the upstream rate limit caps ingest at one page
per minute. Zero-ops, file-backed, trivially backed up. If this ever becomes a
multi-user hosted service, the upgrade path is Postgres; nothing in the schema
prevents it.

## Tests

```bash
uv run pytest      # 9 rule tests, pure in-memory SQLite
uv run ruff check .
```

Engines are pure functions over plain dicts — every rule is unit-testable
without network or fixtures.
