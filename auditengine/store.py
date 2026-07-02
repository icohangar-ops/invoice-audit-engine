"""Storage and ingestion for the invoice audit engine.

Invoices land here either from a live Precoro sync (rate-limited) or from
previously exported JSON pages (offline import), so audits are re-runnable
without touching the API.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from auditengine.db import connect, init_schema

DDL = """
CREATE TABLE IF NOT EXISTS audit_invoices (
    id INTEGER PRIMARY KEY,
    idn TEXT,
    invoice_number TEXT,
    supplier_id INTEGER,
    supplier_name TEXT,
    issue_date TEXT,
    create_date TEXT,
    required_date TEXT,
    sum REAL,
    net_sum REAL,
    sum_paid REAL,
    status INTEGER,
    currency TEXT,
    raw JSON
);
CREATE TABLE IF NOT EXISTS audit_items (
    invoice_id INTEGER,
    name TEXT,
    price REAL,
    quantity REAL,
    line_sum REAL,
    tax_percent REAL,
    PRIMARY KEY (invoice_id, name, price)
);
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    rule TEXT,
    severity TEXT,
    supplier_name TEXT,
    invoice_number TEXT,
    detail TEXT,
    amount REAL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    init_schema(conn, DDL)


def upsert_invoice(conn: sqlite3.Connection, inv: dict[str, Any]) -> None:
    sup = inv.get("supplier") or {}
    conn.execute(
        """INSERT INTO audit_invoices
           (id, idn, invoice_number, supplier_id, supplier_name, issue_date, create_date,
            required_date, sum, net_sum, sum_paid, status, currency, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             invoice_number=excluded.invoice_number, sum=excluded.sum,
             sum_paid=excluded.sum_paid, status=excluded.status, raw=excluded.raw""",
        (
            inv["id"],
            str(inv.get("idn") or ""),
            str(inv.get("invoiceNumber") or ""),
            sup.get("id"),
            sup.get("name") or "",
            (inv.get("issueDate") or "")[:10],
            (inv.get("createDate") or "")[:10],
            (inv.get("requiredDate") or "")[:10],
            float(inv.get("sum") or 0),
            float(inv.get("netSum") or 0),
            float(inv.get("sumPaid") or 0),
            inv.get("status"),
            inv.get("currency") or "USD",
            json.dumps(inv, default=str),
        ),
    )


def upsert_items(conn: sqlite3.Connection, invoice_id: int, detail: dict[str, Any]) -> int:
    """Extract line items from an invoice-detail payload (nested dicts keyed by id)."""
    count = 0

    def walk(node: Any) -> None:
        nonlocal count
        if isinstance(node, dict):
            if "name" in node and ("price" in node or "quantity" in node):
                conn.execute(
                    """INSERT OR REPLACE INTO audit_items
                       (invoice_id, name, price, quantity, line_sum, tax_percent)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        invoice_id,
                        str(node.get("name") or ""),
                        _f(node.get("price")),
                        _f(node.get("quantity")),
                        _f(node.get("sum")),
                        _f(node.get("taxPercent") or node.get("tax")),
                    ),
                )
                count += 1
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(detail.get("items"))
    return count


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def import_json_pages(paths: list[Path]) -> int:
    """Offline import of exported /invoices pages or single-invoice detail files."""
    n = 0
    with connect() as conn:
        ensure_schema(conn)
        for p in paths:
            body = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(body, dict) and "data" in body:
                for inv in body["data"]:
                    upsert_invoice(conn, inv)
                    n += 1
            elif isinstance(body, dict) and "id" in body:
                upsert_invoice(conn, body)
                upsert_items(conn, body["id"], body)
                n += 1
            elif isinstance(body, list):  # pre-flattened list of invoice dicts
                for inv in body:
                    if "id" in inv:
                        upsert_invoice(conn, inv)
                        n += 1
    return n
