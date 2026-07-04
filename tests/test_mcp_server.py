"""The MCP server registers the audit rules as callable tools.

Feeds synthetic invoice rows exhibiting a known duplicate-number pattern; runs
fully in-memory (no Precoro/network). Skipped cleanly when the optional ``mcp``
SDK is not installed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from auditengine import mcp_server  # noqa: E402


def _tool_names() -> set[str]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {t.name for t in tools}


def test_expected_tools_registered() -> None:
    assert _tool_names() >= {"audit_invoices", "normalize_invoice_number"}


def test_normalize_tool_collides_typos() -> None:
    assert mcp_server.normalize_invoice_number("#INV20481") == \
        mcp_server.normalize_invoice_number("NV20481")


def test_audit_flags_duplicate_numbers() -> None:
    invoices = [
        {
            "id": 1, "invoice_number": "INV100", "supplier_id": 7,
            "supplier_name": "Vendor 7", "issue_date": "2026-01-01",
            "create_date": "2026-01-02", "required_date": "2026-01-01",
            "sum": 500.0, "sum_paid": 0.0, "status": 2,
        },
        {
            "id": 2, "invoice_number": "#INV100", "supplier_id": 7,
            "supplier_name": "Vendor 7", "issue_date": "2026-01-05",
            "create_date": "2026-01-06", "required_date": "2026-01-05",
            "sum": 500.0, "sum_paid": 0.0, "status": 2,
        },
    ]
    findings = mcp_server.audit_invoices(invoices)
    assert isinstance(findings, list)
    rules = {f["rule"] for f in findings}
    assert "duplicate_invoice_number" in rules
