"""CHP decision layer: R0 refusal, deterministic foundation scoring, human lock,
and the decision ledger.

Covers the consensus-hardening-protocol integration (``auditengine.chp_gate``):

- the finding-shaped R0 gate refuses ill-posed actions before anything persists;
- the deterministic adversary scores the bounded rule pass + snapshot backing +
  invoice-data parity (the finance floor is 100, and a parity mismatch is fatal);
- hold-grade (high severity) findings open ``PROVISIONAL_LOCK`` and need a named
  confirmer to reach ``LOCKED``; advisory flags pass with documented parity;
- holds, flags, and refusals all seal a CHP payload envelope into the
  append-only decision ledger, whose reads re-validate integrity;
- the finding→decision integration matches the CLI/web action path exactly.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from chp import Verdict
from chp.models import DecisionCase, SessionStatus

from auditengine import store
from auditengine.chp_gate import ChpRejection, InvoiceChpGate, is_hold_grade
from auditengine.config import settings
from auditengine.rules import AuditConfig, Finding, persist, run_all

CONFIRMER = "sam@cubiczan.com"
CFG = AuditConfig()


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    return c


def _gate(tmp_path: Path, lock_mode: str | None = "") -> InvoiceChpGate:
    s = dataclasses.replace(
        settings,
        chp_decisions_path=tmp_path / "chp_decisions.jsonl",
        chp_require_human_lock=lock_mode,
    )
    return InvoiceChpGate(s)


def _inv(
    c: sqlite3.Connection,
    id: int,
    num: str,
    issue: str,
    created: str,
    total: float,
    paid: float = 0.0,
    status: int = 2,
    supplier: int = 1,
) -> None:
    store.upsert_invoice(
        c,
        {
            "id": id,
            "idn": str(id),
            "invoiceNumber": num,
            "supplier": {"id": supplier, "name": f"Vendor {supplier}"},
            "issueDate": issue,
            "createDate": created,
            "requiredDate": issue,
            "sum": total,
            "netSum": total,
            "sumPaid": paid,
            "status": status,
            "currency": "USD",
        },
    )


def _item(
    c: sqlite3.Connection,
    inv_id: int,
    name: str,
    price: float,
    tax: float | None = None,
    line_sum: float | None = None,
) -> None:
    c.execute(
        "INSERT OR REPLACE INTO audit_items VALUES (?,?,?,?,?,?)",
        (inv_id, name, price, None, line_sum or price, tax),
    )


def _seed(conn: sqlite3.Connection) -> None:
    """Two vendors producing a hold-grade duplicate and an advisory entry lag."""
    _inv(conn, 1, "INV100", "2026-03-01", "2026-03-02", 500)
    _inv(conn, 2, "#INV100", "2026-03-05", "2026-03-06", 500)  # duplicate of id 1
    _inv(conn, 3, "B1", "2026-03-01", "2026-03-20", 900)  # 19 days lag -> med


def _duplicate_finding(conn: sqlite3.Connection) -> Finding:
    return next(f for f in run_all(conn) if f.rule == "duplicate_invoice_number")


def _lag_finding(conn: sqlite3.Connection) -> Finding:
    return next(f for f in run_all(conn) if f.rule == "entry_lag")


# ----------------------------------------------------------------------- R0


def test_r0_refuses_a_zero_exposure_advisory_finding(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-03-01", "2026-03-02", 500)
    gate = _gate(Path("/tmp/unused"))
    finding = Finding("inconsistent_tax", "med", "Vendor 1", "A1", "no exposure", 0.0)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(conn, finding, CFG)
    assert excinfo.value.evaluation is not None
    assert excinfo.value.evaluation.results["Worth_it"] == "FATAL"
    assert excinfo.value.evaluation.verdict == Verdict.HALT


def test_r0_refuses_an_unknown_rule_as_unsolvable(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-03-01", "2026-03-02", 500)
    gate = _gate(Path("/tmp/unused"))
    finding = Finding("made_up_rule", "med", "Vendor 1", "A1", "detail", 10.0)
    with pytest.raises(ChpRejection, match="Solvable"):
        gate.open_r0(conn, finding, CFG)


def test_r0_refuses_a_finding_without_invoice_backing(conn: sqlite3.Connection) -> None:
    _inv(conn, 1, "A1", "2026-03-01", "2026-03-02", 500)
    gate = _gate(Path("/tmp/unused"))
    finding = Finding("entry_lag", "med", "Vendor 1", "MISSING-999", "detail", 10.0)
    with pytest.raises(ChpRejection, match="Valid"):
        gate.open_r0(conn, finding, CFG)


def test_r0_accepts_a_real_finding(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    evaluation = gate.open_r0(conn, _duplicate_finding(conn), CFG)
    assert evaluation.verdict == Verdict.PASS
    assert set(evaluation.results.values()) == {"PASS"}


# ---------------------------------------------------------------- foundation


def test_real_finding_scores_a_full_finance_foundation(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    assessment = gate.assess_foundation(conn, _duplicate_finding(conn), CFG)
    assert assessment.domain == "finance"
    assert assessment.score == 100
    assert assessment.parity is not None
    assert assessment.parity.within_tolerance is True
    assert assessment.parity.evidence["group_size"] == 2


def test_finance_floor_fails_below_100_without_parity(conn: sqlite3.Connection) -> None:
    """Parity unavailable -> score 70 -> the action cannot self-certify."""
    _inv(conn, 1, "A1", "not-a-date", "also-bad", 500)
    gate = _gate(Path("/tmp/unused"))
    finding = Finding("entry_lag", "high", "Vendor 1", "A1", "detail", 500.0)
    assessment = gate.assess_foundation(conn, finding, CFG)
    assert assessment.score == 70
    assert assessment.parity is None
    # hold-grade: refused without a confirmer, but a named confirmer may
    # lock it through the human lock (REFRAME never self-certifies).
    refused = gate.gate_findings(conn, [finding], CFG)
    assert refused.admitted == []
    assert "human lock required" in refused.refusals[0].reason
    outcome = gate.gate_findings(conn, [finding], CFG, confirmed_by=CONFIRMER)
    assert len(outcome.admitted) == 1
    assert outcome.entries[0]["session_status"] == SessionStatus.LOCKED.value


def test_parity_mismatch_is_fatal(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    real = _duplicate_finding(conn)
    fabricated = Finding(
        real.rule,
        real.severity,
        real.supplier_name,
        real.invoice_number,
        real.detail,
        999_999.0,  # exposure not real in the invoice data
    )
    assert is_hold_grade(fabricated)
    with pytest.raises(ChpRejection, match="MISMATCH"):
        gate.harden(conn, fabricated, CFG)
    outcome = gate.gate_findings(conn, [fabricated], CFG, confirmed_by=CONFIRMER)
    assert outcome.admitted == []
    assert outcome.refusals[0].reason.startswith("CHP foundation")
    assert outcome.entries[0]["session_status"] == SessionStatus.REFRAME_REQUIRED.value


def test_parity_verifies_all_rules(conn: sqlite3.Connection) -> None:
    """Every rule's own findings must re-verify against the invoice snapshot."""
    for i, month in enumerate(["01", "02", "03", "04"], start=1):
        _inv(conn, i, f"A{i}", f"2026-{month}-01", f"2026-{month}-21", 1000)  # 20d lag
        _item(conn, i, "Hourly Service", 150)
    _inv(conn, 9, "A9", "2026-05-01", "2026-05-01", 9000)
    _item(conn, 9, "Hourly Service", 150)
    _item(conn, 9, "fuel adjustment", 263.25)
    _item(conn, 9, "cleanup credit", -40.0, line_sum=-40.0)
    _item(conn, 9, "Disposal (BBL)", 1.5, tax=8.625)
    _inv(conn, 10, "A10", "2026-06-01", "2026-06-01", 2000, paid=500, status=2)
    _item(conn, 10, "Disposal (BBL)", 2.25, tax=None)  # +50% on the baseline
    findings = run_all(conn)
    assert findings, "the synthetic data must produce findings"
    gate = _gate(Path("/tmp/unused"))
    outcome = gate.gate_findings(conn, findings, CFG, confirmed_by=CONFIRMER)
    rules_admitted = {f.rule for f in outcome.admitted}
    assert outcome.refusals == []
    assert len(outcome.admitted) == len(findings)
    assert {
        "entry_lag",
        "overdue_unpaid",
        "amount_outlier",
        "rate_change",
        "new_charge_type",
        "unexplained_credit",
        "inconsistent_tax",
    } <= rules_admitted


# ----------------------------------------------------------------- lock flow


def test_hold_grade_finding_requires_a_named_confirmer(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    outcome = gate.gate_findings(conn, [_duplicate_finding(conn)], CFG)
    assert outcome.admitted == []
    assert len(outcome.refusals) == 1
    assert "human lock required" in outcome.refusals[0].reason


def test_named_confirmer_locks_the_hold(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    finding = _duplicate_finding(conn)
    outcome = gate.gate_findings(conn, [finding], CFG, confirmed_by=CONFIRMER)
    assert len(outcome.admitted) == 1
    assert outcome.refusals == []
    assert outcome.entries[0]["action"] == "hold"
    assert outcome.entries[0]["session_status"] == SessionStatus.LOCKED.value
    assert outcome.entries[0]["confirmed_by"] == CONFIRMER


def test_hold_opens_provisional_before_locking(conn: sqlite3.Connection) -> None:
    """Sessions start EXPLORING; harden opens PROVISIONAL_LOCK; a confirmer locks."""
    fresh = DecisionCase(decision_id="t", title="t", domain="finance", created_at="now", owner="t")
    assert fresh.status == SessionStatus.EXPLORING
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    decision = gate.harden(conn, _duplicate_finding(conn), CFG)
    assert decision.case.status == SessionStatus.PROVISIONAL_LOCK
    assert gate.lock(decision, CONFIRMER) == SessionStatus.LOCKED


def test_advisory_flag_passes_with_documented_parity(conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate = _gate(Path("/tmp/unused"))
    finding = _lag_finding(conn)
    assert finding.severity == "med"
    outcome = gate.gate_findings(conn, [finding], CFG)
    assert len(outcome.admitted) == 1
    assert outcome.refusals == []
    assert outcome.entries[0]["action"] == "flag"
    assert outcome.entries[0]["session_status"] == SessionStatus.PROVISIONAL_LOCK.value
    assert outcome.entries[0]["confirmed_by"] is None


# ------------------------------------------------------- human-lock policy


def test_require_human_lock_refuses_advisory_flags_too(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed(conn)
    gate = _gate(tmp_path, lock_mode="1")
    refused = gate.gate_findings(conn, [_lag_finding(conn)], CFG)
    assert refused.admitted == []
    assert "human lock required" in refused.refusals[0].reason
    outcome = gate.gate_findings(conn, [_lag_finding(conn)], CFG, confirmed_by=CONFIRMER)
    assert outcome.entries[0]["session_status"] == SessionStatus.LOCKED.value


def test_lock_policy_off_admits_unconfirmed_holds(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(conn)
    gate = _gate(tmp_path, lock_mode="0")
    outcome = gate.gate_findings(conn, [_duplicate_finding(conn)], CFG)
    assert len(outcome.admitted) == 1
    assert outcome.refusals == []
    assert outcome.entries[0]["session_status"] == SessionStatus.PROVISIONAL_LOCK.value


# -------------------------------------------------------------------- ledger


def gated(gate: InvoiceChpGate, conn: sqlite3.Connection) -> None:
    _seed(conn)
    gate.gate_findings(conn, run_all(conn), CFG, confirmed_by=CONFIRMER)


def test_ledger_round_trip_and_integrity(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gated(gate, conn)

    listing = gate.records.list()
    # _seed: 1 duplicate (high) + 1 entry lag (med) + 3 overdue-unpaid (high)
    assert len(listing) == 5
    assert all(r["envelope_valid"] is True for r in listing)
    assert all(r["integrity_valid"] is True for r in listing)
    assert all(len(r["body_sha256"]) == 64 for r in listing)

    got = gate.records.get(listing[0]["decision_id"])
    assert got is not None and got["rule"] == listing[0]["rule"]
    assert gate.records.get("audit-missing") is None


def test_tampered_ledger_reads_as_integrity_invalid(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    gate = _gate(tmp_path)
    gated(gate, conn)

    path = gate.records.path
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    # tamper with the sealed payload body: deflate the adversary score
    entry["body"] = entry["body"].replace('"foundation_score": 100', '"foundation_score": 40')
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    record = next(r for r in gate.records.list() if r["decision_id"] == entry["decision_id"])
    assert record["integrity_valid"] is False
    assert record["envelope_valid"] is True  # the CHP envelope checks structure only


def test_refusals_are_recorded_alongside_holds_and_flags(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Default policy: the hold is refused and recorded; the advisory flag passes."""
    _seed(conn)
    gate = _gate(tmp_path)
    findings = run_all(conn)
    outcome = gate.gate_findings(conn, findings, CFG)

    actions = {r["action"] for r in gate.records.list()}
    assert actions == {"refusal", "flag"}
    refusal = next(
        r
        for r in gate.records.list()
        if r["action"] == "refusal" and r["rule"] == "duplicate_invoice_number"
    )
    assert "human lock required" in refusal["reason"]
    assert len(outcome.admitted) == 1  # only the advisory lag flag


# ------------------------------------------------- finding->decision pipeline


def test_full_pipeline_matches_the_action_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """run_all -> gate -> persist: exactly what cli.run and web /run execute."""
    _seed(conn)
    gate = _gate(tmp_path)
    findings = run_all(conn, CFG)
    outcome = gate.gate_findings(conn, findings, CFG, confirmed_by=CONFIRMER)
    persist(conn, outcome.admitted)

    rows = conn.execute("SELECT rule FROM audit_findings").fetchall()
    assert {r["rule"] for r in rows} == {f.rule for f in outcome.admitted}
    records = gate.records.list()
    assert len(records) == len(findings)
    assert all(r["action"] in {"hold", "flag"} for r in records)
    assert all(r["r0_verdict"] == Verdict.PASS.value for r in records)
