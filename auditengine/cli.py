"""CLI: sync, import, audit, and the CHP decision ledger — without the web UI.

Usage:
    python -m auditengine.cli sync [--max-pages N]
    python -m auditengine.cli import <folder-of-json-pages>
    python -m auditengine.cli run [--confirmed-by email]
    python -m auditengine.cli decisions [--limit N] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from auditengine import store
from auditengine.chp_gate import InvoiceChpGate
from auditengine.config import settings
from auditengine.db import connect
from auditengine.precoro import PrecoroClient
from auditengine.rules import DEFAULT_CONFIG, persist, run_all


def _audit(confirmed_by: str = "") -> None:
    """The finding→action path: rules run, CHP gates the flag, the gate admits
    or refuses. Only admitted findings reach the AP action queue (audit_findings);
    every refusal is sealed into the decision ledger with its reason."""
    with connect() as conn:
        store.ensure_schema(conn)
        findings = run_all(conn, DEFAULT_CONFIG)
        gate = InvoiceChpGate(settings)
        outcome = gate.gate_findings(
            conn, findings, DEFAULT_CONFIG, confirmed_by=confirmed_by or None
        )
        persist(conn, outcome.admitted)
    held = sum(1 for e in outcome.entries if e["action"] == "hold")
    print(
        f"{len(findings)} findings -> {len(outcome.admitted)} actioned by CHP "
        f"({held} hold-grade, {len(outcome.admitted) - held} advisory), "
        f"{len(outcome.refusals)} refused"
    )
    for refusal in outcome.refusals:
        f = refusal.finding
        print(f"  refused [{f.rule}] {f.supplier_name} {f.invoice_number}: {refusal.reason}")


def _decisions(limit: int, as_json: bool) -> None:
    gate = InvoiceChpGate(settings)
    records = gate.records.list(limit=limit)
    if as_json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return
    if not records:
        print("no decision records yet — run the audit first")
        return
    print(
        f"{'decision_id':<40} {'action':<9} {'status':<18} {'score':>5}  "
        f"{'confirmed_by':<24} integrity"
    )
    for r in records:
        score = r["foundation_score"] if r["foundation_score"] is not None else "-"
        print(
            f"{r['decision_id']:<40} {r['action']:<9} {r['session_status']:<18} "
            f"{score!s:>5}  {(r['confirmed_by'] or '-'):<24} "
            f"{'ok' if r['integrity_valid'] else 'TAMPERED'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auditengine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="pull invoices from Precoro (rate-limited)")
    p_sync.add_argument("--max-pages", type=int, default=15)
    p_imp = sub.add_parser("import", help="import exported /invoices JSON pages")
    p_imp.add_argument("folder", type=Path)
    p_run = sub.add_parser("run", help="re-run audit rules over stored invoices")
    p_run.add_argument(
        "--confirmed-by",
        default="",
        help="email of the human confirming hold-grade (high severity) flags",
    )
    p_dec = sub.add_parser("decisions", help="show the CHP decision ledger")
    p_dec.add_argument("--limit", type=int, default=20)
    p_dec.add_argument("--json", action="store_true", help="full records as JSON")
    args = parser.parse_args(argv)

    if args.cmd == "decisions":
        _decisions(args.limit, args.json)
        return 0

    if args.cmd == "sync":
        client = PrecoroClient()
        n = 0
        with connect() as conn:
            store.ensure_schema(conn)
            for inv in client.iter_invoices(max_pages=args.max_pages):
                store.upsert_invoice(conn, inv)
                conn.commit()
                n += 1
        print(f"synced {n} invoices")
    elif args.cmd == "import":
        n = store.import_json_pages(sorted(args.folder.glob("*.json")))
        print(f"imported {n} invoice records")

    _audit(confirmed_by=getattr(args, "confirmed_by", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
