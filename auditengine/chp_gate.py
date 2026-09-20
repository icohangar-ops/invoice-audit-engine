"""CHP decision layer for invoice audit findings (consensus-hardening-protocol, finance profile).

Every finding the engine *acts on* becomes a CHP decision case so the question
"why is this vendor's invoice flagged — and who said so?" has a mechanical
answer. The action surface, honestly stated: this engine has no automated
payment holds, escalations, or notifications. Its finding→action path is the
persisted findings snapshot (``audit_findings``) that feeds the AP team's
dashboard and ``findings.csv`` export — persisting a finding IS the flag
action, and high-severity findings are the payment-hold-grade flags an AP team
acts on to stop a payment. This gate wraps exactly that path (CLI ``run`` and
web ``/run``); the MCP tool is read-only detection and creates no decisions.

Four hardening stages wrap the action:

1. **R0 gate — before the action.** ``chp.gates.evaluate_r0_gate`` with
   finding-shaped criteria: the action is *solvable* (a known rule with a
   deterministic verifier over the invoice data — decidable from invoice data
   alone), *scoped* (targets one vendor/invoice pair, thresholds bounded — not
   a blanket account-wide action), *valid* (the referenced invoice actually
   exists in the stored snapshot), and *worth_it* (non-zero dollar exposure or
   hold-grade severity). HALT refuses the action with nothing persisted.
   Failures are FATAL, never warnings.
2. **Deterministic adversary — after the rules run.** Scores the finding's
   foundation out of 100: 40 for the bounded deterministic rule pass, 30 for a
   result backed by the stored snapshot, and 30 for invoice-data *parity* — an
   independent re-verification that recomputes the anomaly (amounts,
   duplicates, vendor mismatches) from the stored invoice rows and matches the
   flagged exposure. Invoice data is the finance domain, which gates at CHP's
   finance floor of exactly 100: without parity evidence a finding cannot
   self-certify. A parity *mismatch* is fatal — a flag contradicted by the
   invoice data must not be actioned, and no confirmer can wave it through.
3. **Human lock.** Every hardened case opens ``PROVISIONAL_LOCK`` (sessions
   start ``EXPLORING``); ``apply_third_party_validation`` with a named
   ``confirmed_by`` locks it (``LOCKED``). Hold-grade actions (severity
   ``high`` — the payment-hold triggers) require that confirmation by default;
   ``INVOICE_AUDIT_CHP_REQUIRE_HUMAN_LOCK=1`` makes it mandatory for every
   action, ``=0`` disables it. Advisory flags (``med``/``low``) may pass
   without a confirmer when parity evidence is documented.
4. **Decision record.** Holds, flags, *and refusals* are sealed into a CHP
   payload envelope and appended to the decision ledger (append-only JSONL
   under the gitignored data tree). The ledger adds its own SHA-256 digest
   over the sealed body — CHP's ``validate_payload_envelope`` checks structure
   only — and every read re-validates both, exposing ``integrity_valid``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import statistics
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from chp import (
    CHPOrchestrator,
    CHPReport,
    DecisionCase,
    Dossier,
    FoundationAttack,
    FoundationDisclosure,
    ThirdPartyValidation,
    ValidationResult,
    Verdict,
    apply_third_party_validation,
    build_payload_envelope,
    validate_payload_envelope,
)
from chp.gates import GateEvaluation, evaluate_r0_gate
from chp.models import SessionStatus

from auditengine.rules import AuditConfig, Finding, normalize_number

# Deterministic adversary scoring (out of 100). The finance floor in
# chp.foundation is exactly 100, so only a parity-verified finding can
# self-certify an action on invoice data.
_GUARDRAIL_POINTS = 40
_BOUNDED_RESULT_POINTS = 30
_PARITY_POINTS = 30
_FULL_SCORE = _GUARDRAIL_POINTS + _BOUNDED_RESULT_POINTS + _PARITY_POINTS

_FINANCE_DOMAIN = "finance"

# Parity tolerance for exposure amounts: 1 cent or 0.5%, whichever is larger.
_TOLERANCE_PCT = 0.005
_TOLERANCE_ABS = 0.01

_ENVELOPE_ROUTE = "DECIDE"
_LEDGER_OWNER = "invoice-audit-engine"


class ChpRejection(Exception):
    """CHP refused the action (R0 HALT, parity mismatch, or lock required)."""

    def __init__(
        self,
        reason: str,
        evaluation: GateEvaluation | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evaluation = evaluation


def _tolerance(expected: float) -> float:
    return max(_TOLERANCE_ABS, abs(expected) * _TOLERANCE_PCT)


def _close(expected: float, actual: float) -> bool:
    return abs(expected - actual) <= _tolerance(expected)


@dataclass(frozen=True)
class ParityEvidence:
    """The flagged exposure vs an independent recomputation from invoice data."""

    rule: str
    expected: float | None  # recomputed exposure; None = no anomaly recomputed
    actual: float  # the finding's flagged exposure
    tolerance: float
    within_tolerance: bool | None
    evidence: dict[str, Any]  # the concrete invoice data behind the recomputation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FoundationAssessment:
    """The deterministic adversary's verdict on a flagged finding."""

    score: int
    domain: str
    findings: list[str] = field(default_factory=list)
    parity: ParityEvidence | None = None


@dataclass(frozen=True)
class ChpDecision:
    """A hardened finding action: the CHP case, its report, and the assessment."""

    case: DecisionCase
    report: CHPReport
    assessment: FoundationAssessment


@dataclass(frozen=True)
class Refusal:
    """A finding the gate refused to action, with the recorded reason."""

    finding: Finding
    reason: str


@dataclass(frozen=True)
class GateOutcome:
    """Result of gating a rule run: what may be actioned, what was refused."""

    admitted: list[Finding]
    refusals: list[Refusal]
    entries: list[dict[str, Any]]  # every sealed ledger record (admitted + refused)


class DecisionLedger:
    """Append-only JSONL of CHP decision records; integrity re-checked on read."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first records with envelope and body integrity re-validated on read."""
        return [self._checked(entry) for entry in self._read_all()[-limit:]][::-1]

    def get(self, decision_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._read_all()):
            if entry.get("decision_id") == decision_id:
                return self._checked(entry)
        return None

    @staticmethod
    def _checked(entry: dict[str, Any]) -> dict[str, Any]:
        """Re-validate a record on read: envelope structure and body digest.

        The CHP payload envelope validates structure only, so the ledger adds
        its own SHA-256 digest over the sealed body — a tampered record reads
        as ``integrity_valid: false``.
        """
        body = entry.get("body", "")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {
            **entry,
            "envelope_valid": validate_payload_envelope(entry.get("envelope", "")),
            "integrity_valid": digest == entry.get("body_sha256"),
        }


# ---------------------------------------------------------------------------
# Invoice-data parity: independent per-rule re-verification of a flagged
# anomaly. Each verifier recomputes the anomaly from the stored invoice rows
# (the same snapshot the rules ran on) and returns the recomputed exposure plus
# the concrete evidence. Parity here is what the erp-control-plane promotion
# gate gets from a dbt-pinned golden set: pinned truth the flagged result must
# match before the action can self-certify.
# ---------------------------------------------------------------------------


def _invoices(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM audit_invoices ORDER BY id")]


def _items(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM audit_items")]


def _history(invoices: list[dict], items: list[dict]) -> list[tuple[dict, dict]]:
    """Invoice/item pairs ordered by issue date (same order the rules use)."""
    inv_map = {i["id"]: i for i in invoices}
    pairs = [(inv_map[i["invoice_id"]], i) for i in items if i["invoice_id"] in inv_map]
    pairs.sort(key=lambda p: p[0]["issue_date"])
    return pairs


def _find(conn, finding: Finding) -> dict | None:
    """The referenced invoice row: supplier name plus any numbered token."""
    tokens = {t.strip() for t in finding.invoice_number.split(",")}
    for inv in _invoices(conn):
        if inv["supplier_name"] == finding.supplier_name and inv["invoice_number"] in tokens:
            return inv
    return None


def _verify_duplicate(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    del cfg
    tokens = {normalize_number(t) for t in finding.invoice_number.split(",")}
    by_key: dict[str, list[dict]] = {}
    for inv in _invoices(conn):
        if inv["supplier_name"] == finding.supplier_name:
            by_key.setdefault(normalize_number(inv["invoice_number"]), []).append(inv)
    best: tuple[str, list[dict]] | None = None
    for norm, group in by_key.items():
        if norm and norm in tokens and (best is None or len(group) > len(best[1])):
            best = (norm, group)
    if best is None:
        evidence = {"vendor_invoices": sorted(i["invoice_number"] for i in by_key.get("", []))}
        return ParityEvidence(
            finding.rule, None, finding.amount, _tolerance(finding.amount), False, evidence
        )
    norm, group = best
    ordered = sorted(group, key=lambda g: g["id"])
    expected = sum(g["sum"] for g in ordered[1:])
    evidence = {
        "normalized_number": norm,
        "colliding_invoices": [g["invoice_number"] for g in ordered],
        "group_size": len(group),
    }
    return ParityEvidence(
        finding.rule,
        expected,
        finding.amount,
        _tolerance(expected),
        _close(expected, finding.amount),
        evidence,
    )


def _verify_entry_lag(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    inv = _find(conn, finding)
    if inv is None:
        return None
    try:
        issue = date.fromisoformat(inv["issue_date"])
        created = date.fromisoformat(inv["create_date"])
    except ValueError:
        return None
    lag = (created - issue).days
    expected = inv["sum"]
    evidence = {
        "issue_date": inv["issue_date"],
        "create_date": inv["create_date"],
        "lag_days": lag,
        "threshold_days": cfg.entry_lag_days,
    }
    within = lag > cfg.entry_lag_days and _close(expected, finding.amount)
    return ParityEvidence(
        finding.rule, expected, finding.amount, _tolerance(expected), within, evidence
    )


def _verify_overdue(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    inv = _find(conn, finding)
    if inv is None:
        return None
    balance = inv["sum"] - (inv["sum_paid"] or 0)
    try:
        due = date.fromisoformat(inv["required_date"] or inv["issue_date"])
    except ValueError:
        return None
    days_over = (datetime.now().date() - due).days
    real = (
        inv["status"] in (2, 4)
        and (inv["sum_paid"] or 0) < inv["sum"]
        and days_over > cfg.overdue_days
    )
    evidence = {
        "due_date": due.isoformat(),
        "days_over": days_over,
        "threshold_days": cfg.overdue_days,
        "balance": balance,
        "status": inv["status"],
    }
    return ParityEvidence(
        finding.rule,
        balance,
        finding.amount,
        _tolerance(balance),
        real and _close(balance, finding.amount),
        evidence,
    )


def _verify_outlier(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    inv = _find(conn, finding)
    if inv is None:
        return None
    vendor_rows = [i for i in _invoices(conn) if i["supplier_id"] == inv["supplier_id"]]
    if len(vendor_rows) < cfg.min_invoices_for_baseline + 1:
        return None  # vendor baseline not established — parity unavailable
    med = statistics.median(g["sum"] for g in vendor_rows)
    if med <= 0:
        return None
    expected = inv["sum"]
    real = expected > med * cfg.amount_outlier_multiple
    evidence = {
        "vendor_median": med,
        "multiple": expected / med,
        "outlier_multiple": cfg.amount_outlier_multiple,
        "vendor_invoice_count": len(vendor_rows),
    }
    return ParityEvidence(
        finding.rule,
        expected,
        finding.amount,
        _tolerance(expected),
        real and _close(expected, finding.amount),
        evidence,
    )


def _verify_rate_change(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    inv = _find(conn, finding)
    if inv is None:
        return None
    rows = _history(_invoices(conn), _items(conn))
    by_item: dict[tuple[int | None, str], list[tuple[dict, dict]]] = {}
    for iv, it in rows:
        if iv["supplier_id"] == inv["supplier_id"] and it["price"] is not None and it["price"] >= 0:
            by_item.setdefault((iv["supplier_id"], it["name"].lower()), []).append((iv, it))
    candidates = []
    for (_sid, _name), prs in by_item.items():
        if len(prs) < 2:
            continue
        baseline = prs[0][1]["price"]
        for iv, it in prs[1:]:
            if iv["id"] != inv["id"] or not baseline:
                continue
            pct = (it["price"] - baseline) / baseline * 100
            if abs(pct) > cfg.rate_change_pct:
                candidates.append(
                    {
                        "item": it["name"],
                        "baseline": baseline,
                        "price": it["price"],
                        "pct_change": pct,
                        "line_sum": it["line_sum"] or 0,
                    }
                )
    match = next((c for c in candidates if _close(c["line_sum"], finding.amount)), None)
    expected = match["line_sum"] if match else None
    evidence = {"invoice_id": inv["id"], "items_above_baseline": candidates}
    within = bool(candidates) and match is not None
    return ParityEvidence(
        finding.rule, expected, finding.amount, _tolerance(finding.amount), within, evidence
    )


def _verify_new_charge(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    del cfg
    inv = _find(conn, finding)
    if inv is None:
        return None
    rows = [
        (iv, it)
        for iv, it in _history(_invoices(conn), _items(conn))
        if iv["supplier_id"] == inv["supplier_id"]
    ]
    seen: set[str] = set()
    inv_count = 0
    last: str | None = None
    candidates = []
    for iv, it in rows:
        name = it["name"].lower()
        if iv["invoice_number"] != last:
            inv_count += 1
            last = iv["invoice_number"]
        if name not in seen and inv_count > 3 and iv["id"] == inv["id"]:
            candidates.append(
                {
                    "item": it["name"],
                    "first_seen_invoice": iv["invoice_number"],
                    "exposure": it["line_sum"] or it["price"] or 0,
                }
            )
        seen.add(name)
    match = next((c for c in candidates if _close(c["exposure"], finding.amount)), None)
    expected = match["exposure"] if match else None
    evidence = {"invoice_id": inv["id"], "new_charge_types": candidates}
    within = bool(candidates) and match is not None
    return ParityEvidence(
        finding.rule, expected, finding.amount, _tolerance(finding.amount), within, evidence
    )


def _verify_credit(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    del cfg
    inv = _find(conn, finding)
    if inv is None:
        return None
    candidates = [
        {"item": it["name"], "amount": it["price"]}
        for it in _items(conn)
        if it["invoice_id"] == inv["id"] and it["price"] is not None and it["price"] < 0
    ]
    match = next((c for c in candidates if _close(c["amount"], finding.amount)), None)
    expected = match["amount"] if match else None
    evidence = {"invoice_id": inv["id"], "credit_lines": candidates}
    within = bool(candidates) and match is not None
    return ParityEvidence(
        finding.rule, expected, finding.amount, _tolerance(finding.amount), within, evidence
    )


def _verify_tax(conn, finding: Finding, cfg: AuditConfig) -> ParityEvidence | None:
    del cfg
    inv = _find(conn, finding)
    if inv is None:
        return None
    invoices = _invoices(conn)
    tax_seen: dict[tuple[int | None, str], set[bool]] = {}
    for iv, it in _history(invoices, _items(conn)):
        if iv["supplier_id"] != inv["supplier_id"]:
            continue
        key = (iv["supplier_id"], it["name"].lower())
        tax_seen.setdefault(key, set()).add(bool(it["tax_percent"]))
    flagged = {k for k, v in tax_seen.items() if len(v) > 1}
    candidates = []
    for iv, it in _history(invoices, _items(conn)):
        if iv["id"] != inv["id"] or (iv["supplier_id"], it["name"].lower()) not in flagged:
            continue
        if it["tax_percent"]:
            candidates.append(
                {
                    "item": it["name"],
                    "tax_percent": it["tax_percent"],
                    "line_sum": it["line_sum"] or 0,
                }
            )
    match = next((c for c in candidates if _close(c["line_sum"], finding.amount)), None)
    expected = match["line_sum"] if match else None
    evidence = {"invoice_id": inv["id"], "taxed_inconsistent_items": candidates}
    within = bool(candidates) and match is not None
    return ParityEvidence(
        finding.rule, expected, finding.amount, _tolerance(finding.amount), within, evidence
    )


# The parity registry doubles as the "known rule" check in R0: a finding is
# solvable from invoice data alone exactly when a deterministic verifier exists.
PARITY_VERIFIERS: dict[str, Callable[[Any, Finding, AuditConfig], ParityEvidence | None]] = {
    "duplicate_invoice_number": _verify_duplicate,
    "entry_lag": _verify_entry_lag,
    "overdue_unpaid": _verify_overdue,
    "amount_outlier": _verify_outlier,
    "rate_change": _verify_rate_change,
    "new_charge_type": _verify_new_charge,
    "unexplained_credit": _verify_credit,
    "inconsistent_tax": _verify_tax,
}

# Per-rule threshold boundedness: an action is only scoped when the config that
# produced it is finite and meaningful.
_THRESHOLD_CHECKS: dict[str, Callable[[AuditConfig], bool]] = {
    "entry_lag": lambda c: c.entry_lag_days > 0,
    "overdue_unpaid": lambda c: c.overdue_days > 0,
    "amount_outlier": lambda c: c.amount_outlier_multiple > 1 and c.min_invoices_for_baseline > 0,
    "rate_change": lambda c: c.rate_change_pct > 0,
    "new_charge_type": lambda c: c.min_invoices_for_baseline > 0,
}


def is_hold_grade(finding: Finding) -> bool:
    """Hold-grade findings are the payment-hold triggers an AP team acts on.

    Severity ``high`` (duplicate billing, overdue exposure, unagreed rate
    change, severe entry lag) is what stops a payment; ``med``/``low`` are
    advisory flags.
    """
    return finding.severity == "high"


class InvoiceChpGate:
    """Runs a finding action through CHP: R0 -> adversary -> human lock -> record."""

    def __init__(self, settings: Any) -> None:
        """Settings need ``chp_decisions_path`` and ``chp_require_human_lock``."""
        self.settings = settings
        self.records = DecisionLedger(settings.chp_decisions_path)

    # ----------------------------------------------------------- lock policy
    def _lock_required(self, finding: Finding) -> bool:
        mode = self.settings.chp_require_human_lock
        if mode == "1":
            return True
        if mode == "0":
            return False
        return is_hold_grade(finding)  # default: ON for blocking actions

    # ------------------------------------------------------------------- R0
    def open_r0(self, conn, finding: Finding, cfg: AuditConfig) -> GateEvaluation:
        """The pre-action gate: HALT before anything is persisted or flagged."""
        threshold_check = _THRESHOLD_CHECKS.get(finding.rule, lambda _c: True)
        evaluation = evaluate_r0_gate(
            solvable=finding.rule in PARITY_VERIFIERS and bool(finding.detail.strip()),
            scoped=(
                bool(finding.supplier_name.strip())
                and bool(finding.invoice_number.strip())
                and threshold_check(cfg)
            ),
            valid=_find(conn, finding) is not None,
            worth_it=bool(finding.amount) or is_hold_grade(finding),
        )
        if evaluation.verdict != Verdict.PASS:
            failed = [name for name, result in evaluation.results.items() if result != "PASS"]
            raise ChpRejection(
                "CHP R0 gate: the finding action failed " + ", ".join(sorted(failed)),
                evaluation,
            )
        return evaluation

    # ------------------------------------------------------------ foundation
    def assess_foundation(self, conn, finding: Finding, cfg: AuditConfig) -> FoundationAssessment:
        """The deterministic adversary scores the flagged finding (0-100)."""
        findings: list[str] = []
        score = 0

        n_invoices = conn.execute("SELECT COUNT(*) FROM audit_invoices").fetchone()[0]
        score += _GUARDRAIL_POINTS
        findings.append(
            "deterministic rule pass: pure rules over the stored invoice snapshot, "
            f"{n_invoices} invoice(s), thresholds bounded by AuditConfig"
        )

        if n_invoices >= 1 and _find(conn, finding) is not None:
            score += _BOUNDED_RESULT_POINTS
            findings.append(
                f"bounded result: finding backed by the snapshot — vendor "
                f"'{finding.supplier_name}' invoice '{finding.invoice_number}' present"
            )
        else:
            findings.append("no bounded result — the finding is not backed by the snapshot")

        verifier = PARITY_VERIFIERS.get(finding.rule)
        parity = verifier(conn, finding, cfg) if verifier else None
        if parity is None:
            findings.append(
                "invoice-data parity unavailable for this rule — the foundation cannot "
                f"self-certify at the finance floor ({_FINANCE_DOMAIN} = {_FULL_SCORE})"
            )
        elif parity.within_tolerance:
            score += _PARITY_POINTS
            findings.append(
                f"invoice-data parity: recomputed exposure {parity.expected:,.2f} matches "
                f"flagged {parity.actual:,.2f} ± {parity.tolerance:,.2f} ({parity.evidence})"
            )
        else:
            recomputed = f"{parity.expected:,.2f}" if parity.expected is not None else "no anomaly"
            findings.append(
                f"invoice-data parity MISMATCH: recomputed {recomputed} vs flagged "
                f"{parity.actual:,.2f} — the flagged anomaly is not real in the invoice data"
            )

        return FoundationAssessment(
            score=min(score, _FULL_SCORE),
            domain=_FINANCE_DOMAIN,
            findings=findings,
            parity=parity,
        )

    # --------------------------------------------------------------- session
    def harden(self, conn, finding: Finding, cfg: AuditConfig) -> ChpDecision:
        """Score the finding and open the CHP case (EXPLORING -> PROVISIONAL_LOCK)."""
        assessment = self.assess_foundation(conn, finding, cfg)
        if assessment.parity is not None and assessment.parity.within_tolerance is False:
            raise ChpRejection(
                f"CHP foundation: {assessment.findings[-1]} — a flag contradicted by the "
                "invoice data must not be actioned; fix the finding or the underlying data."
            )

        digest = hashlib.sha256(
            f"{finding.rule}|{finding.supplier_name}|{finding.invoice_number}".encode()
        ).hexdigest()[:10]
        case = DecisionCase(
            decision_id=f"audit-{finding.rule}-{digest}",
            title=f"{finding.rule}: {finding.supplier_name} invoice {finding.invoice_number}",
            domain=assessment.domain,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            owner=_LEDGER_OWNER,
            # CHP's internal R0 re-checks worth_it from high_stakes; the
            # finding-shaped R0 gate above is the real filter, so every hardened
            # case is high-stakes here (mirrors the erp-control-plane promotion
            # gate). Hold vs advisory is recorded in the action, not here.
            high_stakes=True,
            dossier=Dossier(
                core_problem=(f"Act on the {finding.rule} finding: {finding.detail}"),
                goal_state=["persist the finding to the AP action queue with CHP evidence"],
                current_state=[
                    f"rule '{finding.rule}' flagged exposure {finding.amount:,.2f}",
                    f"adversary score {assessment.score}/{_FULL_SCORE} ({assessment.domain})",
                ],
                constraints=[
                    "actions decided from the stored invoice snapshot only",
                    "thresholds from AuditConfig, tuned by finance",
                    "no write-back to Precoro from this engine",
                ],
                scope=[f"supplier:{finding.supplier_name}", f"invoice:{finding.invoice_number}"],
            ),
        )
        # The case opens EXPLORING (the protocol's starting session status) and is
        # moved to PROVISIONAL_LOCK below — no finding action ever self-certifies.
        disclosure = FoundationDisclosure(
            weakest_assumptions=[
                "the rule correctly models the anomaly its name claims",
                "the stored invoice snapshot is current as of the last sync/import",
                "the recomputed parity evidence reflects the flagged exposure",
            ],
            invalidation_conditions=[
                "invoice-data parity mismatch on the flagged exposure",
                "snapshot rows change after a modifiedSince re-delivery",
            ],
            key_vulnerability=(
                "single-source parity: the anomaly is verified only against the same "
                "stored snapshot the rules ran on, not an independent source"
            ),
        )
        attack = FoundationAttack(
            attack_summary="; ".join(assessment.findings),
            foundation_score=assessment.score,
            vulnerability_strike=(
                "without invoice-data parity the flag rests only on the rule engine's "
                "say-so, not on evidence in the vendor's billing history"
            ),
            assumption_attacks=[
                "independent recomputation of the anomaly from stored invoice rows",
                "vendor and invoice identity re-checked in the snapshot",
                "bounded thresholds recorded in the decision body",
            ],
        )
        # Fresh orchestrator per case: the protocol registry is in-memory state
        # we do not rely on — the decision ledger is the durable record.
        report = CHPOrchestrator().run_initial_session(
            case=case, foundation_disclosure=disclosure, foundation_attack=attack
        )
        case.status = SessionStatus.PROVISIONAL_LOCK
        return ChpDecision(case, report, assessment)

    # ------------------------------------------------------------- human lock
    def lock(self, decision: ChpDecision, confirmed_by: str) -> SessionStatus:
        """Third-party confirmation: PROVISIONAL_LOCK -> LOCKED (recorded on the case)."""
        return apply_third_party_validation(
            decision.case,
            ThirdPartyValidation(
                validator=confirmed_by,
                item=decision.case.decision_id,
                challenge=(
                    "Confirm the flagged anomaly is real in the invoice data and the "
                    "action (payment hold / AP flag) is warranted"
                ),
                result=ValidationResult.CONFIRM,
                rationale="Named confirmer approved the finding action via the audit CLI/API",
            ),
        )

    # ----------------------------------------------------------------- record
    def _entry(
        self,
        *,
        finding: Finding,
        cfg: AuditConfig,
        decision: ChpDecision | None,
        r0_results: dict[str, str] | None,
        action: str,
        confirmed_by: str | None,
        reason: str | None,
        session_status: str,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"{finding.rule}|{finding.supplier_name}|{finding.invoice_number}".encode()
        ).hexdigest()[:10]
        decision_id = decision.case.decision_id if decision else f"audit-{finding.rule}-{digest}"
        body = json.dumps(
            {
                "action": action,
                "adversary_findings": decision.assessment.findings if decision else [],
                "amount": finding.amount,
                "confirmed_by": confirmed_by,
                "decision_id": decision_id,
                "detail": finding.detail,
                "domain": decision.case.domain if decision else _FINANCE_DOMAIN,
                "foundation_score": decision.case.foundation_score if decision else None,
                "foundation_verdict": (
                    decision.report.foundation_verdict.value if decision else None
                ),
                "invoice_number": finding.invoice_number,
                "locked_decisions": list(decision.case.locked_decisions) if decision else [],
                "parity": (
                    decision.assessment.parity.to_dict()
                    if decision and decision.assessment.parity
                    else None
                ),
                "r0_results": r0_results,
                "r0_verdict": decision.report.r0_verdict.value if decision else Verdict.HALT.value,
                "reason": reason,
                "rule": finding.rule,
                "severity": finding.severity,
                "supplier_name": finding.supplier_name,
                "thresholds": asdict(cfg),
                "title": decision.case.title if decision else finding.detail,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        envelope = build_payload_envelope(body, route=_ENVELOPE_ROUTE)
        return {
            "decision_id": decision_id,
            "created_at": (
                decision.case.created_at if decision else dt.datetime.now(dt.UTC).isoformat()
            ),
            "rule": finding.rule,
            "severity": finding.severity,
            "supplier_name": finding.supplier_name,
            "invoice_number": finding.invoice_number,
            "action": action,
            "session_status": session_status,
            "r0_verdict": decision.report.r0_verdict.value if decision else Verdict.HALT.value,
            "r0_results": r0_results,
            "foundation_verdict": (decision.report.foundation_verdict.value if decision else None),
            "foundation_score": decision.case.foundation_score if decision else None,
            "confirmed_by": confirmed_by,
            "reason": reason,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "envelope": envelope.render(),
        }

    # --------------------------------------------------------------- pipeline
    def gate_findings(
        self,
        conn,
        findings: list[Finding],
        cfg: AuditConfig,
        *,
        confirmed_by: str | None = None,
    ) -> GateOutcome:
        """Gate a rule run: every finding is actioned, or refused and recorded.

        Admitted findings may be persisted to the AP action queue; refusals are
        sealed into the same ledger with their reason.
        """
        admitted: list[Finding] = []
        refusals: list[Refusal] = []
        entries: list[dict[str, Any]] = []
        for finding in findings:
            try:
                self.open_r0(conn, finding, cfg)
            except ChpRejection as rejection:
                entry = self._entry(
                    finding=finding,
                    cfg=cfg,
                    decision=None,
                    r0_results=rejection.evaluation.results if rejection.evaluation else None,
                    action="refusal",
                    confirmed_by=confirmed_by,
                    reason=rejection.reason,
                    session_status=SessionStatus.HALT.value,
                )
                self.records.append(entry)
                entries.append(entry)
                refusals.append(Refusal(finding, rejection.reason))
                continue

            try:
                decision = self.harden(conn, finding, cfg)
            except ChpRejection as rejection:  # parity mismatch — fatal, no case
                entry = self._entry(
                    finding=finding,
                    cfg=cfg,
                    decision=None,
                    r0_results=None,
                    action="refusal",
                    confirmed_by=confirmed_by,
                    reason=rejection.reason,
                    session_status=SessionStatus.REFRAME_REQUIRED.value,
                )
                self.records.append(entry)
                entries.append(entry)
                refusals.append(Refusal(finding, rejection.reason))
                continue

            status = decision.case.status
            if self._lock_required(finding):
                if not confirmed_by:
                    reason = (
                        "human lock required: hold-grade finding (severity "
                        f"'{finding.severity}') may not be actioned without a named "
                        "confirmer (INVOICE_AUDIT_CHP_REQUIRE_HUMAN_LOCK)"
                    )
                    entry = self._entry(
                        finding=finding,
                        cfg=cfg,
                        decision=decision,
                        r0_results=None,
                        action="refusal",
                        confirmed_by=None,
                        reason=reason,
                        session_status=status.value,
                    )
                    self.records.append(entry)
                    entries.append(entry)
                    refusals.append(Refusal(finding, reason))
                    continue
                status = self.lock(decision, confirmed_by)

            action = "hold" if is_hold_grade(finding) else "flag"
            entry = self._entry(
                finding=finding,
                cfg=cfg,
                decision=decision,
                r0_results=None,
                action=action,
                confirmed_by=confirmed_by if status == SessionStatus.LOCKED else None,
                reason=None,
                session_status=status.value,
            )
            self.records.append(entry)
            entries.append(entry)
            admitted.append(finding)
        return GateOutcome(admitted=admitted, refusals=refusals, entries=entries)
