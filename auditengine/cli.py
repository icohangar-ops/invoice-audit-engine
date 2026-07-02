"""CLI: sync, import, and audit without the web UI.

Usage:
    python -m auditengine.cli sync [--max-pages N]
    python -m auditengine.cli import <folder-of-json-pages>
    python -m auditengine.cli run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from auditengine import store
from auditengine.db import connect
from auditengine.precoro import PrecoroClient
from auditengine.rules import DEFAULT_CONFIG, persist, run_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auditengine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="pull invoices from Precoro (rate-limited)")
    p_sync.add_argument("--max-pages", type=int, default=15)
    p_imp = sub.add_parser("import", help="import exported /invoices JSON pages")
    p_imp.add_argument("folder", type=Path)
    sub.add_parser("run", help="re-run audit rules over stored invoices")
    args = parser.parse_args(argv)

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

    with connect() as conn:
        store.ensure_schema(conn)
        findings = run_all(conn, DEFAULT_CONFIG)
        persist(conn, findings)
    high = sum(1 for f in findings if f.severity == "high")
    print(f"{len(findings)} findings ({high} high severity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
