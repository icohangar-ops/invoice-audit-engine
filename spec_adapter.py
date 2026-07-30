#!/usr/bin/env python3
"""Cross-language parity adapter for the invoice audit rules.

These eight anomaly rules also exist as TypeScript in `@cubiczan/finance-engines`.
Nothing previously proved the two flagged the same invoices, so a rule could drift
and an anomaly caught by one stack could pass unnoticed in the other. This adapter
lets the finance-engines parity suite check that claim.

The donor engine reads rows out of SQLite; the rules themselves take plain lists
of dicts, so this adapter passes the in-memory rows straight through with no
database involved.

Run from the finance-engines checkout:

    python3 spec/run_parity.py \
        --adapter-cmd "python3 ../invoice-audit-engine/spec_adapter.py" \
        --suite audit --suite pyformat

Protocol: one JSON request per line on stdin, one response per line on stdout.
"""
from __future__ import annotations

import dataclasses
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auditengine import rules as R  # noqa: E402


def decode(value):
    if isinstance(value, str):
        if value == "Infinity":
            return math.inf
        if value == "-Infinity":
            return -math.inf
        if value == "NaN":
            return math.nan
        return value
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    return value


def encode(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return encode(dataclasses.asdict(value))
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if value == math.inf:
            return "Infinity"
        if value == -math.inf:
            return "-Infinity"
        return value
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


CONFIG_FIELDS = (
    "entry_lag_days",
    "overdue_days",
    "amount_outlier_multiple",
    "rate_change_pct",
    "min_invoices_for_baseline",
)


def to_config(cfg: dict | None) -> R.AuditConfig:
    cfg = cfg or {}
    return R.AuditConfig(**{k: cfg[k] for k in CONFIG_FIELDS if k in cfg})


def pin_today(cfg: dict | None):
    """Freeze the clock inside the rules module for reproducibility.

    The donor rules read the current date via `datetime.now().date()`, so an
    overdue or entry-lag check would otherwise depend on the day the suite runs
    and could never match a fixed vector. The TypeScript port takes an injectable
    `today`; this achieves the same by shadowing `datetime` for one call.

    Note it is `datetime` that gets shadowed, not `date` — `date.fromisoformat`
    is used for parsing in the same module and must keep working.
    """
    iso = (cfg or {}).get("today")
    if not iso:
        return None
    pinned = date.fromisoformat(iso)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return datetime(pinned.year, pinned.month, pinned.day)

        @classmethod
        def today(cls):  # noqa: D102
            return cls.now()

    original = R.datetime
    R.datetime = _FrozenDatetime
    return original


def with_today(fn):
    """Run `fn` with the clock pinned, always restoring the module afterwards."""
    def wrapper(args):
        original = pin_today(args.get("cfg"))
        try:
            return fn(args)
        finally:
            if original is not None:
                R.datetime = original
    return wrapper


def _n(args):
    return -0.0 if args.get("negative_zero") else args["n"]


def pyformat_fixed(args) -> str:
    n = _n(args)
    decimals = args["decimals"]
    if args.get("grouping"):
        return f"{n:,.{decimals}f}"
    if args.get("force_sign"):
        return f"{n:+.{decimals}f}"
    return f"{n:.{decimals}f}"


OPS = {
    "pyformat.fixed": pyformat_fixed,
    "pyformat.signed_pct": lambda a: f"{_n(a):+.0%}",
    "pyformat.round2": lambda a: round(_n(a), 2),
    "pyformat.g": lambda a: f"{_n(a):g}",

    "audit.normalize_number": lambda a: R.normalize_number(a["num"]),
    "audit.duplicate_numbers": lambda a: R.duplicate_numbers(a["invoices"]),
    "audit.entry_lag": with_today(lambda a: R.entry_lag(a["invoices"], to_config(a.get("cfg")))),
    "audit.overdue_unpaid": with_today(
        lambda a: R.overdue_unpaid(a["invoices"], to_config(a.get("cfg")))
    ),
    "audit.amount_outliers": lambda a: R.amount_outliers(a["invoices"], to_config(a.get("cfg"))),
    "audit.rate_changes": lambda a: R.rate_changes(
        a["invoices"], a["items"], to_config(a.get("cfg"))
    ),
    "audit.new_charge_types": lambda a: R.new_charge_types(a["invoices"], a["items"]),
    "audit.negative_adjustments": lambda a: R.negative_adjustments(a["invoices"], a["items"]),
    "audit.inconsistent_tax": lambda a: R.inconsistent_tax(a["invoices"], a["items"]),
}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            fn = OPS.get(req["op"])
            if fn is None:
                out = {"ok": False, "error": "unsupported op"}
            else:
                out = {"ok": True, "result": encode(fn(decode(req.get("args", {}))))}
        except Exception as exc:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
