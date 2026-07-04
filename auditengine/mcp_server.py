"""MCP server for the invoice audit engine.

Exposes the engine's deterministic anomaly rules as Model Context Protocol
tools. Thin wrapper — all detection logic lives in ``auditengine.rules`` and is
reused verbatim; nothing here touches Precoro or the network. Invoice/item rows
are supplied by the caller and loaded into an in-memory SQLite database (the
same schema the engine uses), so audits run fully offline.

Follows the same publishing path proven by codesentinel / codehealth-mcp:
namespace ``io.github.Cubiczan``, stdio transport, published via the
``mcp-publisher`` CLI (see the "MCP Server" section of the README).

Run it:

    uvx --from invoice-audit-engine invoice-audit-mcp
    # or, from a checkout:
    python -m auditengine.mcp_server
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from auditengine import store
from auditengine.rules import AuditConfig, normalize_number, run_all

mcp = FastMCP(
    "invoice-audit-engine",
    instructions=(
        "Continuous vendor-invoice anomaly detection for procure-to-pay. Supply "
        "invoice and (optionally) line-item rows; the tools flag duplicate "
        "numbers, entry lag, overdue-unpaid, amount outliers, unit-rate changes, "
        "new charge types, unexplained credits, and inconsistent tax. "
        "Deterministic and offline — rows run through an in-memory SQLite copy."
    ),
)

# Column order for the audit_invoices / audit_items tables (see auditengine.store.DDL).
_INVOICE_COLS = [
    "id", "idn", "invoice_number", "supplier_id", "supplier_name",
    "issue_date", "create_date", "required_date", "sum", "net_sum",
    "sum_paid", "status", "currency", "raw",
]
_ITEM_COLS = ["invoice_id", "name", "price", "quantity", "line_sum", "tax_percent"]


def _load(invoices: list[dict[str, Any]], items: list[dict[str, Any]] | None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    for inv in invoices:
        conn.execute(
            f"INSERT OR REPLACE INTO audit_invoices ({','.join(_INVOICE_COLS)}) "
            f"VALUES ({','.join('?' * len(_INVOICE_COLS))})",
            [inv.get(c) for c in _INVOICE_COLS],
        )
    for it in items or []:
        conn.execute(
            f"INSERT OR REPLACE INTO audit_items ({','.join(_ITEM_COLS)}) "
            f"VALUES ({','.join('?' * len(_ITEM_COLS))})",
            [it.get(c) for c in _ITEM_COLS],
        )
    return conn


@mcp.tool()
def audit_invoices(
    invoices: list[dict[str, Any]],
    items: list[dict[str, Any]] | None = None,
    entry_lag_days: int = 14,
    overdue_days: int = 10,
    amount_outlier_multiple: float = 3.0,
    rate_change_pct: float = 5.0,
    min_invoices_for_baseline: int = 3,
) -> list[dict[str, Any]]:
    """Run all anomaly rules over a set of invoices (and optional line items).

    Header-level rules always run; item-level rules run only for invoices whose
    line items are supplied. Returns findings with rule, severity, supplier,
    invoice number, human-readable detail, and dollar exposure.

    Args:
        invoices: Invoice rows. Expected keys mirror the engine schema:
            id, invoice_number, supplier_id, supplier_name, issue_date,
            create_date, required_date, sum, sum_paid, status.
        items: Optional line-item rows: invoice_id, name, price, quantity,
            line_sum, tax_percent.
        entry_lag_days: Days between issue and entry before flagging entry lag.
        overdue_days: Days past due before flagging an approved-but-unpaid invoice.
        amount_outlier_multiple: Multiple of a vendor's median to flag as an outlier.
        rate_change_pct: Percent unit-price change to flag for a recurring item.
        min_invoices_for_baseline: Minimum invoices per vendor before outlier logic runs.
    """
    cfg = AuditConfig(
        entry_lag_days=entry_lag_days,
        overdue_days=overdue_days,
        amount_outlier_multiple=amount_outlier_multiple,
        rate_change_pct=rate_change_pct,
        min_invoices_for_baseline=min_invoices_for_baseline,
    )
    conn = _load(invoices, items)
    try:
        return [asdict(f) for f in run_all(conn, cfg)]
    finally:
        conn.close()


@mcp.tool()
def normalize_invoice_number(number: str) -> str:
    """Normalize an invoice number for duplicate detection.

    Strips punctuation, uppercases, and drops common prefixes so that
    'INV123', '#INV123', and 'NV123' collide to the same canonical form.

    Args:
        number: A raw invoice number as printed by the vendor.
    """
    return normalize_number(number)


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
