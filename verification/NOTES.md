# invoice-audit-engine — Lean 4 verification notes

Model: `verification/Audit.lean` (Lean 4.34.1, **core library only**, no
Mathlib). Compiles with plain `~/.elan/bin/lean Audit.lean`, exit 0, no
`sorry`/`admit`/custom axioms. No source files were modified.

The model follows the **code** (`auditengine/rules.py`, `auditengine/web.py`,
`auditengine/store.py`), not the README. All rule functions were modelled as
implemented, including their quirks; quirks are proved, not smoothed over.

## Modelling decisions

- **Money is `Int` (cents)** in the model; the code uses Python floats
  throughout (SQLite `REAL` columns). Percentage and median comparisons are
  cross-multiplied into exact integer form (equivalent over the rationals
  whenever the divisor is positive, which the code's guards ensure on the
  flagging path). Consequence: the model's `Config` holds
  `outlierMultiple`/`rateChangePct` as `Int` — the code's defaults (3.0, 5.0)
  are integral, but a fractional config value (e.g. multiple 2.5) is
  *unrepresentable* in the model and would need `Rat`.
- **Dates are `Int` day-numbers**; `today` is a parameter (the code calls
  `datetime.now()`). Date *parse failures* — which the code handles by
  silently skipping the invoice (`try/except` in `entry_lag`; an exception in
  `overdue_unpaid`, see Risks) — are outside the model.
- `Row` is an item already joined to its invoice (items with a missing invoice
  are dropped by `_item_history`, and `negative_adjustments` skips them too).
  `Row.name` is pre-lowercased (the code keys on `name.lower()`); `Row.tax`
  records the tax rate but only its zero-ness is ever observed by the rules.
- Duplicate findings store the *normalized* number in the model; the code
  stores `", ".join(sorted({raw numbers}))`. Amounts/severities match.
- `runAll` takes the item rows twice — `histRows` (date-sorted history order,
  consumed by rate/new-charge/tax) and `rawRows` (raw DB order, consumed by
  negative adjustments) — because the code itself iterates them differently.

## Theorem → source mapping

Source: `auditengine/rules.py` unless noted. "Iff" = soundness **and**
completeness: the finding exists iff the stated condition holds, with the
finding's fields uniquely determined.

### Normalization (`normalize_number`, ll. 36–40)

| Theorem | What it proves |
|---|---|
| `normalize_chars` | Every character of `normalize s` came from `s` through the ASCII upper-map and lies in `A–Z`/`0–9`. |
| `normalize_collision_inv` | `"#INV20481"` and `"NV20481"` normalize to the same key (the repo's own test case). |
| `normalize_collision_case` | `"INV-123"` = `"inv123"` after normalization. |
| `normalize_in_prefix` | `"IN100"` normalizes to `"100"` — the `IN` prefix is stripped too. |
| `normalize_empty` | `"---"` normalizes to `[]` (see `dup_none_of_empty_norm`). |
| `normalize_not_idempotent` | `normalize "INVINV1" = "INV1" ≠ "1"` — the prefix is stripped **once**, so `"INVINV1"` and `"INV1"` do *not* collide. |

### Rule 1 — `duplicate_numbers` (ll. 59–81)

| Theorem | What it proves |
|---|---|
| `mem_dupGroup` | Group membership iff same supplier **and** same normalized number. |
| `mem_dupFindings` | A finding is emitted iff its key's group has ≥ 2 members and a nonempty normalized number. |
| `dup_amount_identity` | Finding amount = sum of the group **excluding the first member in list order**; equivalently `amount + first.total = group total`. |
| `dup_none_of_empty_norm` | A group whose numbers normalize to `""` is **never** flagged, however many copies exist. |
| `dup_none_of_small_group` | Singleton groups are never flagged. |
| `dup_pytest_example` | Mirrors `tests/test_rules.py`: one finding for the INV-20481/NV20481 pair. |
| `dup_supplier_separation` | Same number under two different suppliers is not a duplicate. |
| `dup_order_dependence` | **Counterexample:** the same two invoices (100, 200) yield finding amount **200 or 100 depending on row order**. |

### Rule 2 — `entry_lag` (ll. 84–104)

| Theorem | What it proves |
|---|---|
| `lagFinding_eq_some` | Iff: flagged iff `created − issued > entry_lag_days` (strict); amount = invoice `sum`; severity `med` iff lag ≤ 45 else `high`. |
| `lag_not_flagged_at_threshold` | Lag exactly 14 (default) is not flagged. |
| `lag_boundaries` | Concrete: lag 14 → none; 15 → `med`; 45 → `med`; 46 → `high`. |

### Rule 3 — `overdue_unpaid` (ll. 107–134)

| Theorem | What it proves |
|---|---|
| `overdueFinding_eq_some` | Iff: not skipped as paid, status ∈ {2, 4}, and `today − due > overdue_days` (strict), `due = required_date or issue_date`; amount = `sum − sum_paid`; severity `high` iff days-over > 60. |
| `overdue_none_of_paid` | A fully-paid invoice (nonzero payment ≥ total) is never flagged. |
| `overdue_boundaries` | Concrete (default cfg): 10 days over → none; 11 → `med`, amount = unpaid balance; 60 → `med`; 61 → `high`. |
| `overdue_zero_and_negative_edges` | A zero-total/zero-paid invoice is **not** skipped (0 is falsy in the code's paid test) and is flagged with amount 0; a negative-total (credit) invoice is flagged with a **negative** amount. |

### Rule 4 — `amount_outliers` (ll. 137–158)

Median machinery: `insertI_perm`, `isort_perm`, `insertI_sorted`,
`isort_sorted` (the sorted list is a permutation of the group totals).

| Theorem | What it proves |
|---|---|
| `outlierFinding_eq_some` | Iff (in exact cross-multiplied form `2·total > twiceMedian · multiple`): group size ≥ `min_baseline + 1`, median > 0, and the strict comparison; severity `med`, amount = `sum`. |
| `outlier_boundary_exact_multiple` | An invoice at exactly 3× the median is **not** flagged. |
| `outlier_pytest_example` | Mirrors the repo test: 4×2000 + 9000 flags only the 9000 invoice. |
| `outlier_median_masking` | **Counterexample to robustness:** totals {100, 10000, 10000, 10000} flag *nothing* — the median (10000) already includes the outliers; the baseline is not leave-one-out. |
| `outlier_small_group` | A group of `min_baseline` (3) invoices is never examined, even with a 999999 outlier. |

### Item history (`_item_history`, ll. 161–168) and Rule 5 — `rate_changes` (ll. 171–198)

| Theorem | What it proves |
|---|---|
| `baselineFor_spec` | The baseline row is the first history row for its `(supplier, item)` key (`find?` = the code's `hist[key][0]`). |
| `rateFinding_eq_some` | Iff: baseline exists, is nonzero, and `pct · baseline < 100 · |price − baseline|` (the strict `> rate_change_pct` test, cross-multiplied); severity **`high`** (ll. 194–195), amount = `line_sum or 0`. Rows with missing/negative prices never enter the history (`rateHistory`, line 174). |
| `rate_boundary` | Exactly +5% is **not** flagged; +6% is. |
| `rate_zero_baseline_disables` | **Counterexample:** if the first price for a key is 0, the rule is disabled for that key *forever* (baseline never updates; `if not baseline: continue`). Later prices 50000, 90000 → no findings. |
| `rate_baseline_never_updates` | Baseline is the first row only: +100% (flagged) then a price only +4% over the *original* (a −48% drop from the previous invoice) is not flagged. |

### Rule 6 — `new_charge_types` (ll. 199–230)

| Theorem | What it proves |
|---|---|
| `nctStepFinding_eq_some` | Step iff: item name first-seen for the supplier **and** the per-supplier invoice counter — incremented when the invoice *number string* changes vs that supplier's previous row — exceeds 3; amount = `orAmount` (the `line_sum or price or 0` chain). |
| `nctRec_sound` | Every emitted finding is a `med` `new_charge_type` finding whose supplier/number/amount come from a single history row. |
| `nct_boundary` | First-seen name on the supplier's 3rd distinct invoice number → not flagged; on the 4th → flagged. |
| `nct_recount_on_repeated_number` | **Counterexample:** number sequence A, B, A counts 3 — the counter counts number *changes*, not invoices, so non-consecutive repeats reach the threshold early. |
| `nct_zero_line_sum_falls_through` | A `line_sum` of 0 reports the **price** instead (Python `or` treats 0 as missing). |

### Rule 7 — `negative_adjustments` (ll. 236–256)

| Theorem | What it proves |
|---|---|
| `creditFinding_eq_some` | Iff: the row's price is negative; finding amount = the (negative) price, severity `low`. Iterates raw item order, not the history. |
| `mem_creditFindings` | Membership form of the same characterization. |

### Rule 8 — `inconsistent_tax` (ll. 259–276)

| Theorem | What it proves |
|---|---|
| `taxRec_sound` | Every tax finding is emitted at a *taxed* row whose `(supplier, item)` key is mixed (both taxed and untaxed rows exist over the **full** history); amount = `line_sum or 0`, severity `med`. |
| `tax_scenarios` | Concrete: taxed-vs-untaxed yields exactly one finding, on the taxed row, even when the untaxed row comes first; consistently taxed → none; two taxed rows for one mixed key → still exactly one finding (one per key, first taxed row). |

### Aggregation — `run_all` (ll. 46–57) and the KPI (`web.py:49`)

| Theorem | What it proves |
|---|---|
| `mem_runAll` | Pipeline iff: a finding is in the output iff at least one of the eight rules produced it (concatenation in code order, **no cross-rule dedup**). |
| `totalFlagged_runAll` | Conservation as computed: `total_flagged = Σ |amount|` over the eight rule outputs — concatenation itself loses/adds nothing. |
| `kpi_double_counts_multi_flagged_invoice` | **Counterexample to per-invoice conservation:** one 1000 invoice flagged by both lag and overdue contributes **2000** to the KPI. |
| `kpi_duplicate_undercounts` | **Counterexample:** two copies of a 500 invoice contribute **500** to the KPI, not 1000 (first copy excluded, `dup_amount_identity`). |
| `kpi_abs_of_negative_amounts` | A −5000 overdue credit invoice **raises** the KPI by 5000 (`abs`). |

Generic list machinery: `mem_dedupAux`, `mem_dedup`, `nodup_dedupAux`
(the model's `dedup` mirrors Python dict-key insertion-order deduplication),
`mem_of_drop`, `stripPrefix_subset`.

## Headline findings (discrepancies & risks)

1. **There is no totals-mismatch rule and no tax recomputation.** Nothing in
   the engine checks an invoice total against the sum of its line items, and
   nothing recomputes a tax amount. `inconsistent_tax` compares only the
   *presence* of a tax rate (`bool(tax_percent)`), never its value: the same
   item taxed at 5% on one invoice and 7% on another is **not** flagged,
   while 8.625% vs missing **is**. Anyone reading the product as "audits
   totals and tax math" is reading more than the code does.
2. **Duplicate amounts are order-dependent and understate exposure**
   (`dup_order_dependence`, `dup_amount_identity`, `kpi_duplicate_undercounts`).
   The finding amount is the group total *minus whichever copy happens to be
   first in the `SELECT *` row order* — an order SQLite does not guarantee.
   The dashboard KPI inherits the understatement.
3. **The KPI sums |amount| over findings, not invoices**
   (`kpi_double_counts_multi_flagged_invoice`, `kpi_abs_of_negative_amounts`).
   One invoice flagged by k rules is counted k times; negative amounts
   (credits, overdue credit invoices) *increase* the "flagged $" figure.
4. **Duplicates with empty normalized numbers are invisible**
   (`dup_none_of_empty_norm`): numbers made only of punctuation, or exactly
   `"INV"`/`"IN"`, normalize to `""` and are skipped by the `and norm` guard,
   so unlimited copies pass.
5. **Normalization quirks** (`normalize_not_idempotent`, `normalize_in_prefix`):
   the prefix is stripped once, non-recursively (`INVINV1` → `INV1`), and the
   `IN` alternative makes `IN100` collide with a genuine `100`, while
   `INV100` also maps to `100` — three different raw forms, one key.
6. **Every threshold is strict** (proved boundary theorems for all four):
   lag = 14, overdue = 10, exactly 3× median, exactly +5% rate change are
   all *unflagged*. Severity boundaries are also sharp: lag 45 `med` / 46
   `high`; overdue 60 `med` / 61 `high`.
7. **Outlier detection is fragile** (`outlier_median_masking`,
   `outlier_small_group`): suppliers with < 4 invoices are never examined,
   groups with median ≤ 0 are skipped, and a majority-outlier group flags
   nothing because the outliers themselves set the median.
8. **Rate baseline is the first row, forever** (`rate_zero_baseline_disables`,
   `rate_baseline_never_updates`): gradual drift away from the first price is
   measured only against day one, and a zero first price permanently disables
   the rule for that item. Negative prices are excluded from the history
   entirely — they surface only as `unexplained_credit` findings.
9. **The new-charge counter counts invoice-number *changes***
   (`nct_recount_on_repeated_number`), per supplier, over item-bearing
   invoices only (an invoice with no items is invisible to it). The docstring
   says "after their first three invoices"; with repeated/non-consecutive
   numbers the threshold can trigger on the 4th *row*. Also `line_sum or
   price or 0` means a genuine 0 line sum is silently replaced by the price.
10. **Overdue depends on wall clock and fragile date parsing**
    (`overdue_zero_and_negative_edges`): `today` is `datetime.now()`, so
    results change daily with no data change. A nonzero `required_date`
    string that fails to parse raises inside the loop and the invoice is
    skipped entirely (no fallback to `issue_date` — the fallback only applies
    when `required_date` is falsy). The paid test is `sum_paid` *truthiness*
    plus `>=`: a zero-total invoice with zero paid is treated as unpaid.
11. **Float arithmetic everywhere.** Medians (even-count mean), percentage
    changes, group sums and the KPI are all Python floats over SQLite `REAL`;
    the model proves the *exact-arithmetic* reading. Near a boundary
    (e.g. a change within float epsilon of 5%), the code's result can differ
    from the exact one. Amounts with > 2^53 cents would also lose integer
    precision — unrealistic here, but the type offers no protection.
12. **Ingest can collapse line items** (`store.py` DDL): `audit_items` has
    `PRIMARY KEY (invoice_id, name, price)`, so two genuinely distinct but
    identical line items on one invoice become one row before any rule runs;
    history-based rules (rate/new-charge/tax) then see a shorter history than
    the source data has.
13. **Case handling is Unicode in the code, ASCII in the model.** Python's
    `str.upper()`/`str.lower()` fold non-ASCII characters (e.g. `ß`, Turkish
    `i`); the model folds ASCII only. For realistic invoice numbers/item
    names (ASCII) the behaviors coincide.
14. **Rule count.** `run_all` wires exactly **8** rule functions; README
    material referring to 9 audit rules does not match this code path (the
    CHP decision gate in `chp_gate.py` is a separate subsystem, out of scope
    for this model).
