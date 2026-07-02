"""Standalone FastAPI app for the invoice audit engine."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from auditengine import store
from auditengine.db import connect
from auditengine.precoro import PrecoroClient
from auditengine.rules import DEFAULT_CONFIG, persist, run_all
from auditengine.ui import kpi, page, table

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    with connect() as conn:
        store.ensure_schema(conn)
        n_inv = conn.execute("SELECT COUNT(*) FROM audit_invoices").fetchone()[0]
        n_sup = conn.execute(
            "SELECT COUNT(DISTINCT supplier_id) FROM audit_invoices"
        ).fetchone()[0]
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
        [f["severity"].upper(), f["rule"], f["supplier_name"], f["invoice_number"],
         f["detail"], f["amount"] or 0]
        for f in findings
    ]
    classes = [f"sev-{f['severity']}" for f in findings]
    actions = (
        '<form class="inline" method="post" action="/run"><button>Re-run rules</button></form> '
        '<form class="inline" method="post" action="/sync">'
        "<button>Sync from Precoro (rate-limited: ~1 page/min)</button></form> "
        '<form class="inline" method="post" action="/import">'
        '<input name="dir" placeholder="folder of exported JSON pages" size="40">'
        "<button>Import JSON</button></form> "
        '<a href="/findings.csv">findings.csv</a>'
    )
    body = f'<div class="kpis">{kpis}</div>{actions}<h2>Findings</h2>' + table(
        ["Sev", "Rule", "Vendor", "Invoice", "Detail", "Amount"], rows, classes
    )
    return page("Vendor Invoice Audit", body)


@router.post("/run")
def run_rules() -> RedirectResponse:
    with connect() as conn:
        store.ensure_schema(conn)
        persist(conn, run_all(conn, DEFAULT_CONFIG))
    return RedirectResponse("/", status_code=303)


@router.post("/import")
def import_json(dir: str) -> RedirectResponse:
    store.import_json_pages(sorted(Path(dir).glob("*.json")))
    return run_rules()


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
        persist(conn, run_all(conn, DEFAULT_CONFIG))


@router.post("/sync")
def sync(background: BackgroundTasks) -> RedirectResponse:
    background.add_task(_sync_job)
    return RedirectResponse("/", status_code=303)


app = FastAPI(title="Invoice Audit Engine")
app.include_router(router)
