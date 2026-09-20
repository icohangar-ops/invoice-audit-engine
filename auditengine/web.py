"""Standalone FastAPI app for the invoice audit engine.

Finding actions run through the CHP gate (auditengine.chp_gate): only admitted
findings reach the AP action queue, and the /decisions page surfaces the
ledger — holds, flags, and refusals with integrity status.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from auditengine import store
from auditengine.chp_gate import InvoiceChpGate
from auditengine.config import settings
from auditengine.db import connect
from auditengine.precoro import PrecoroClient
from auditengine.rules import DEFAULT_CONFIG, persist, run_all
from auditengine.ui import kpi, page, table

router = APIRouter()


def _gated_audit(confirmed_by: str | None) -> None:
    """The finding→action path with CHP: admit or refuse each finding, persist
    the admitted ones, and seal every decision (including refusals) in the ledger."""
    with connect() as conn:
        store.ensure_schema(conn)
        findings = run_all(conn, DEFAULT_CONFIG)
        gate = InvoiceChpGate(settings)
        outcome = gate.gate_findings(
            conn, findings, DEFAULT_CONFIG, confirmed_by=confirmed_by or None
        )
        persist(conn, outcome.admitted)


@router.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    with connect() as conn:
        store.ensure_schema(conn)
        n_inv = conn.execute("SELECT COUNT(*) FROM audit_invoices").fetchone()[0]
        n_sup = conn.execute("SELECT COUNT(DISTINCT supplier_id) FROM audit_invoices").fetchone()[0]
        findings = conn.execute(
            "SELECT * FROM audit_findings ORDER BY CASE severity WHEN 'high' THEN 0 "
            "WHEN 'med' THEN 1 ELSE 2 END, amount DESC"
        ).fetchall()
    total_flagged = sum(abs(f["amount"] or 0) for f in findings)
    kpis = (
        kpi("Invoices analyzed", f"{n_inv:,}")
        + kpi("Vendors", f"{n_sup:,}")
        + kpi("Open findings", f"{len(findings):,}")
        + kpi("$ flagged", f"${total_flagged:,.0f}")
    )
    rows = [
        [
            f["severity"].upper(),
            f["rule"],
            f["supplier_name"],
            f["invoice_number"],
            f["detail"],
            f["amount"] or 0,
        ]
        for f in findings
    ]
    classes = [f"sev-{f['severity']}" for f in findings]
    actions = (
        '<form class="inline" method="post" action="/run">'
        '<input name="confirmed_by" placeholder="confirmer email (hold-grade flags)" size=36>'
        "<button>Re-run rules</button></form> "
        '<form class="inline" method="post" action="/sync">'
        "<button>Sync from Precoro (rate-limited: ~1 page/min)</button></form> "
        '<form class="inline" method="post" action="/import">'
        '<input name="dir" placeholder="folder of exported JSON pages" size="40">'
        "<button>Import JSON</button></form> "
        '<a href="/decisions">CHP decisions</a> '
        '<a href="/findings.csv">findings.csv</a>'
    )
    body = f'<div class="kpis">{kpis}</div>{actions}<h2>Findings</h2>' + table(
        ["Sev", "Rule", "Vendor", "Invoice", "Detail", "Amount"], rows, classes
    )
    body += (
        "<p>Findings above are the CHP-admitted actions (holds and advisory flags); "
        'refused findings and their reasons live in the <a href="/decisions">decision '
        "ledger</a>.</p>"
    )
    return page("Vendor Invoice Audit", body)


@router.post("/run")
async def run_rules(request: Request) -> RedirectResponse:
    form = await request.form()
    _gated_audit(str(form.get("confirmed_by") or "").strip())
    return RedirectResponse("/", status_code=303)


@router.post("/import")
def import_json(dir: str) -> RedirectResponse:
    store.import_json_pages(sorted(Path(dir).glob("*.json")))
    _gated_audit(None)
    return RedirectResponse("/", status_code=303)


@router.get("/decisions", response_class=HTMLResponse)
def decisions() -> str:
    gate = InvoiceChpGate(settings)
    records = gate.records.list(limit=200)
    n_hold = sum(1 for r in records if r["action"] == "hold")
    n_refused = sum(1 for r in records if r["action"] == "refusal")
    n_bad = sum(1 for r in records if not r["integrity_valid"])
    kpis = (
        kpi("Decision records", f"{len(records):,}")
        + kpi("Hold-grade actioned", f"{n_hold:,}")
        + kpi("Refusals", f"{n_refused:,}")
        + kpi("Integrity failures", f"{n_bad:,}")
    )
    rows = [
        [
            r["created_at"][:19],
            r["rule"],
            r["supplier_name"],
            r["invoice_number"],
            r["action"],
            r["session_status"],
            r["foundation_score"] if r["foundation_score"] is not None else "",
            r["confirmed_by"] or "",
            "ok" if r["integrity_valid"] else "TAMPERED",
        ]
        for r in records
    ]
    classes = ["" if r["integrity_valid"] else "sev-high" for r in records]
    body = (
        f'<div class="kpis">{kpis}</div>'
        "<p>Append-only CHP decision ledger (JSONL). Holds, advisory flags, and "
        "refusals are all recorded; envelope and body integrity are re-validated on "
        "every read.</p>"
        + table(
            [
                "Created",
                "Rule",
                "Vendor",
                "Invoice",
                "Action",
                "Session",
                "Score",
                "Confirmed by",
                "Integrity",
            ],
            rows,
            classes,
        )
    )
    return page("CHP Decision Ledger", body)


@router.get("/findings.csv", response_class=PlainTextResponse)
def findings_csv() -> str:
    with connect() as conn:
        store.ensure_schema(conn)
        rows = conn.execute("SELECT * FROM audit_findings").fetchall()
    out = ["severity,rule,vendor,invoice,amount,detail"]
    for r in rows:
        detail = (r["detail"] or "").replace(",", ";")
        vendor = (r["supplier_name"] or "").replace(",", " ")
        out.append(
            f"{r['severity']},{r['rule']},{vendor},{r['invoice_number']},"
            f"{r['amount'] or 0},{detail}"
        )
    return "\n".join(out)


def _sync_job(max_pages: int = 15) -> None:
    client = PrecoroClient()
    with connect() as conn:
        store.ensure_schema(conn)
        for inv in client.iter_invoices(max_pages=max_pages):
            store.upsert_invoice(conn, inv)
            conn.commit()
    _gated_audit(None)


@router.post("/sync")
def sync(background: BackgroundTasks) -> RedirectResponse:
    background.add_task(_sync_job)
    return RedirectResponse("/", status_code=303)


app = FastAPI(title="Invoice Audit Engine")
app.include_router(router)
