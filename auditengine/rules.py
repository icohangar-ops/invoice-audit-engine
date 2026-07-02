"""Anomaly rules for vendor invoices.

Header-level rules always run; item-level rules run for invoices whose line
items have been synced. Every rule emits Finding rows persisted for review.
Thresholds live in AuditConfig so finance can tune them without code changes.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class AuditConfig:
    entry_lag_days: int = 14
    overdue_days: int = 10
    amount_outlier_multiple: float = 3.0
    rate_change_pct: float = 5.0
    min_invoices_for_baseline: int = 3


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # high | med | low
    supplier_name: str
    invoice_number: str
    detail: str
    amount: float = 0.0


def normalize_number(num: str) -> str:
    """Normalize an invoice number for duplicate detection: strip punctuation,
    uppercase, and drop common prefixes so 'INV123', '#INV123' and 'NV123' collide."""
    n = re.sub(r"[^A-Z0-9]", "", num.upper())
    return re.sub(r"^(INV|NV|IN)", "", n)


DEFAULT_CONFIG = AuditConfig()


def run_all(conn: sqlite3.Connection, cfg: AuditConfig = DEFAULT_CONFIG) -> list[Finding]:
    invoices = [dict(r) for r in conn.execute("SELECT * FROM audit_invoices").fetchall()]
    items = [dict(r) for r in conn.execute("SELECT * FROM audit_items").fetchall()]
    findings: list[Finding] = []
    findings += duplicate_numbers(invoices)
    findings += entry_lag(invoices, cfg)
    findings += overdue_unpaid(invoices, cfg)
    findings += amount_outliers(invoices, cfg)
    findings += rate_changes(invoices, items, cfg)
    findings += new_charge_types(invoices, items)
    findings += negative_adjustments(invoices, items)
    findings += inconsistent_tax(invoices, items)
    return findings


def _by_id(invoices: list[dict]) -> dict[int, dict]:
    return {i["id"]: i for i in invoices}


def duplicate_numbers(invoices: list[dict]) -> list[Finding]:
    seen: dict[tuple[int | None, str], list[dict]] = {}
    for inv in invoices:
        key = (inv["supplier_id"], normalize_number(inv["invoice_number"]))
        seen.setdefault(key, []).append(inv)
    out = []
    for (_, norm), group in seen.items():
        if len(group) > 1 and norm:
            nums = ", ".join(sorted({g["invoice_number"] for g in group}))
            out.append(
                Finding(
                    "duplicate_invoice_number",
                    "high",
                    group[0]["supplier_name"],
                    nums,
                    f"{len(group)} invoices normalize to the same number '{norm}' "
                    f"— possible duplicate billing/payment",
                    sum(g["sum"] for g in group[1:]),
                )
            )
    return out


def entry_lag(invoices: list[dict], cfg: AuditConfig) -> list[Finding]:
    out = []
    for inv in invoices:
        try:
            issue = date.fromisoformat(inv["issue_date"])
            created = date.fromisoformat(inv["create_date"])
        except ValueError:
            continue
        lag = (created - issue).days
        if lag > cfg.entry_lag_days:
            out.append(
                Finding(
                    "entry_lag",
                    "med" if lag <= 45 else "high",
                    inv["supplier_name"],
                    inv["invoice_number"],
                    f"Entered into Precoro {lag} days after issue date {inv['issue_date']} "
                    f"— period costs understated in the interim",
                    inv["sum"],
                )
            )
    return out


def overdue_unpaid(invoices: list[dict], cfg: AuditConfig) -> list[Finding]:
    out = []
    today = datetime.now().date()
    for inv in invoices:
        if inv["sum_paid"] and inv["sum_paid"] >= inv["sum"]:
            continue
        if inv["status"] not in (2, 4):  # approved / partly paid
            continue
        try:
            due = date.fromisoformat(inv["required_date"] or inv["issue_date"])
        except ValueError:
            continue
        days_over = (today - due).days
        if days_over > cfg.overdue_days:
            out.append(
                Finding(
                    "overdue_unpaid",
                    "high" if days_over > 60 else "med",
                    inv["supplier_name"],
                    inv["invoice_number"],
                    f"Approved but unpaid {days_over} days past {due.isoformat()} "
                    f"— late-fee exposure accruing",
                    inv["sum"] - inv["sum_paid"],
                )
            )
    return out


def amount_outliers(invoices: list[dict], cfg: AuditConfig) -> list[Finding]:
    by_supplier: dict[int | None, list[dict]] = {}
    for inv in invoices:
        by_supplier.setdefault(inv["supplier_id"], []).append(inv)
    out = []
    for group in by_supplier.values():
        if len(group) < cfg.min_invoices_for_baseline + 1:
            continue
        med = statistics.median(g["sum"] for g in group)
        if med <= 0:
            continue
        for inv in group:
            if inv["sum"] > med * cfg.amount_outlier_multiple:
                out.append(
                    Finding(
                        "amount_outlier",
                        "med",
                        inv["supplier_name"],
                        inv["invoice_number"],
                        f"${inv['sum']:,.2f} is {inv['sum'] / med:.1f}x this vendor's "
                        f"median invoice (${med:,.2f})",
                        inv["sum"],
                    )
                )
    return out


def _item_history(invoices: list[dict], items: list[dict]) -> list[tuple[dict, dict]]:
    inv_map = _by_id(invoices)
    pairs = []
    for it in items:
        inv = inv_map.get(it["invoice_id"])
        if inv:
            pairs.append((inv, it))
    pairs.sort(key=lambda p: p[0]["issue_date"])
    return pairs


def rate_changes(invoices: list[dict], items: list[dict], cfg: AuditConfig) -> list[Finding]:
    """Flag unit-price changes for a recurring (supplier, item) pair."""
    history: dict[tuple[int | None, str], list[tuple[dict, dict]]] = {}
    for inv, it in _item_history(invoices, items):
        if it["price"] is None or it["price"] < 0:
            continue
        history.setdefault((inv["supplier_id"], it["name"].lower()), []).append((inv, it))
    out = []
    for (_, _name), rows in history.items():
        if len(rows) < 2:
            continue
        baseline = rows[0][1]["price"]
        for inv, it in rows[1:]:
            if baseline and abs(it["price"] - baseline) / baseline * 100 > cfg.rate_change_pct:
                out.append(
                    Finding(
                        "rate_change",
                        "high",
                        inv["supplier_name"],
                        inv["invoice_number"],
                        f"'{it['name']}' billed at {it['price']:g} vs baseline {baseline:g} "
                        f"({(it['price'] - baseline) / baseline * 100:+.0f}%) — verify amendment",
                        (it["line_sum"] or 0),
                    )
                )
    return out


def new_charge_types(invoices: list[dict], items: list[dict]) -> list[Finding]:
    """Flag item names appearing for a supplier for the first time after their
    first three invoices (surcharges tend to creep in mid-relationship)."""
    seen: dict[int | None, set[str]] = {}
    inv_count: dict[int | None, int] = {}
    out = []
    last_inv: dict[int | None, str] = {}
    for inv, it in _item_history(invoices, items):
        sid = inv["supplier_id"]
        name = it["name"].lower()
        if inv["invoice_number"] != last_inv.get(sid):
            inv_count[sid] = inv_count.get(sid, 0) + 1
            last_inv[sid] = inv["invoice_number"]
        known = seen.setdefault(sid, set())
        if name not in known and inv_count[sid] > 3:
            out.append(
                Finding(
                    "new_charge_type",
                    "med",
                    inv["supplier_name"],
                    inv["invoice_number"],
                    f"New charge type '{it['name']}' first appears on this invoice "
                    f"— confirm it was contractually agreed",
                    it["line_sum"] or it["price"] or 0,
                )
            )
        known.add(name)
    return out


def negative_adjustments(invoices: list[dict], items: list[dict]) -> list[Finding]:
    inv_map = _by_id(invoices)
    out = []
    for it in items:
        if it["price"] is not None and it["price"] < 0:
            inv = inv_map.get(it["invoice_id"])
            if inv is None:
                continue
            out.append(
                Finding(
                    "unexplained_credit",
                    "low",
                    inv["supplier_name"],
                    inv["invoice_number"],
                    f"Credit line '{it['name']}' of {it['price']:,.2f} — adjustments "
                    f"should carry a documented reason",
                    it["price"],
                )
            )
    return out


def inconsistent_tax(invoices: list[dict], items: list[dict]) -> list[Finding]:
    """Same item taxed on some invoices and not others for the same supplier."""
    tax_seen: dict[tuple[int | None, str], set[bool]] = {}
    rows = _item_history(invoices, items)
    for inv, it in rows:
        key = (inv["supplier_id"], it["name"].lower())
        tax_seen.setdefault(key, set()).add(bool(it["tax_percent"]))
    out = []
    flagged = {k for k, v in tax_seen.items() if len(v) > 1}
    for inv, it in rows:
        key = (inv["supplier_id"], it["name"].lower())
        if key in flagged and it["tax_percent"]:
            out.append(
                Finding(
                    "inconsistent_tax",
                    "med",
                    inv["supplier_name"],
                    inv["invoice_number"],
                    f"'{it['name']}' taxed at {it['tax_percent']:g}% here but untaxed on "
                    f"other invoices — verify taxability",
                    it["line_sum"] or 0,
                )
            )
            flagged.discard(key)  # one finding per (supplier, item)
    return out


def persist(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    conn.execute("DELETE FROM audit_findings")
    conn.executemany(
        "INSERT INTO audit_findings (rule, severity, supplier_name, invoice_number, detail, amount)"
        " VALUES (?,?,?,?,?,?)",
        [(f.rule, f.severity, f.supplier_name, f.invoice_number, f.detail, f.amount)
         for f in findings],
    )
