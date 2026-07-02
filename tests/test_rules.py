"""Unit tests for audit rules using a synthetic field-services vendor that
exhibits the billing patterns this engine is designed to catch."""

from __future__ import annotations

import sqlite3

import pytest

from auditengine import store
from auditengine.rules import AuditConfig, normalize_number, run_all


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    return c


def _inv(c: sqlite3.Connection, id: int, num: str, issue: str, created: str,
         total: float, paid: float = 0.0, status: int = 2, supplier: int = 1) -> None:
    store.upsert_invoice(
        c,
        {
            "id": id, "idn": str(id), "invoiceNumber": num,
            "supplier": {"id": supplier, "name": f"Vendor {supplier}"},
            "issueDate": issue, "createDate": created, "requiredDate": issue,
            "sum": total, "netSum": total, "sumPaid": paid, "status": status,
            "currency": "USD",
        },
    )


def _item(c: sqlite3.Connection, inv_id: int, name: str, price: float,
          tax: float | None = None, line_sum: float | None = None) -> None:
    c.execute(
        "INSERT OR REPLACE INTO audit_items VALUES (?,?,?,?,?,?)",
        (inv_id, name, price, None, line_sum or price, tax),
    )


def test_normalize_number_collides_typos() -> None:
    assert normalize_number("#INV20481") == normalize_number("NV20481")
    assert normalize_number("INV-123") == normalize_number("inv123")


def test_duplicate_detection(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "INV100", "2026-01-01", "2026-01-02", 500)
    _inv(conn, 2, "#INV100", "2026-01-05", "2026-01-06", 500)
    assert "duplicate_invoice_number" in [f.rule for f in run_all(conn)]


def test_entry_lag_flagged(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-02-17", "2026-05-08", 3565)  # ~11 weeks late
    fs = [f for f in run_all(conn) if f.rule == "entry_lag"]
    assert len(fs) == 1 and fs[0].severity == "high"


def test_rate_change_flagged(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-02-01", "2026-02-02", 1000)
    _inv(conn, 2, "A2", "2026-03-27", "2026-03-28", 1000)
    _item(conn, 1, "Disposal (BBL)", 1.00)
    _item(conn, 2, "Disposal (BBL)", 1.50)  # unagreed 50% jump
    fs = [f for f in run_all(conn) if f.rule == "rate_change"]
    assert len(fs) == 1
    assert "+50%" in fs[0].detail


def test_new_charge_type_after_baseline(conn: sqlite3.Connection) -> None:
    for i, month in enumerate(["01", "02", "03", "04"], start=1):
        _inv(conn, i, f"A{i}", f"2026-{month}-01", f"2026-{month}-02", 1000)
        _item(conn, i, "Hourly Service", 150)
    _inv(conn, 5, "A5", "2026-05-01", "2026-05-02", 1000)
    _item(conn, 5, "Hourly Service", 150)
    _item(conn, 5, "fuel adjustment", 263.25)  # surcharge creep
    fs = [f for f in run_all(conn) if f.rule == "new_charge_type"]
    assert [f.invoice_number for f in fs] == ["A5"]


def test_inconsistent_tax(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-03-27", "2026-03-28", 1000)
    _inv(conn, 2, "A2", "2026-04-15", "2026-04-16", 1000)
    _item(conn, 1, "Disposal (BBL)", 1.5, tax=8.625)
    _item(conn, 2, "Disposal (BBL)", 1.5, tax=None)
    assert len([f for f in run_all(conn) if f.rule == "inconsistent_tax"]) == 1


def test_overdue_unpaid(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-02-06", "2026-02-07", 4840, paid=0, status=2)
    fs = [f for f in run_all(conn) if f.rule == "overdue_unpaid"]
    assert len(fs) == 1 and fs[0].severity == "high"


def test_paid_invoice_not_overdue(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-02-06", "2026-02-07", 4840, paid=4840, status=5)
    assert not [f for f in run_all(conn) if f.rule == "overdue_unpaid"]


def test_amount_outlier(conn: sqlite3.Connection) -> None:
    for i in range(1, 5):
        _inv(conn, i, f"A{i}", f"2026-0{i}-01", f"2026-0{i}-02", 2000)
    _inv(conn, 9, "A9", "2026-05-01", "2026-05-02", 9000)
    fs = [f for f in run_all(conn, AuditConfig()) if f.rule == "amount_outlier"]
    assert [f.invoice_number for f in fs] == ["A9"]
