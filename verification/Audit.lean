/-!
# Formal model of the invoice audit engine (`auditengine/rules.py`)

Lean 4.34.1, core library only. Money is modelled as `Int` (cents) and dates as
`Int` day-numbers, so every comparison the Python code performs in IEEE-754
floats is modelled *exactly*; float divergence is discussed in `NOTES.md`.
Percentages and medians are cross-multiplied into integer form, which is
equivalent to the float computation over the rationals whenever the divisor
is positive (guarded in the code).

The rules are transcribed from `auditengine/rules.py` (line numbers refer to
that file). Aggregation (`totalFlagged`) transcribes `auditengine/web.py:49`.
-/

namespace InvoiceAudit

/-! ## Basic types -/

inductive Rule where
  | dup | lag | overdue | outlier | rate | newCharge | credit | tax
  deriving DecidableEq, Repr

inductive Severity where
  | high | med | low
  deriving DecidableEq, Repr

/-- `AuditConfig` (rules.py:17–23). The Python fields are floats
(`amount_outlier_multiple = 3.0`, `rate_change_pct = 5.0`); the shipped
defaults are whole numbers and are modelled as `Int`. -/
structure Config where
  entryLagDays : Int := 14
  overdueDays : Int := 10
  outlierMultiple : Int := 3
  rateChangePct : Int := 5
  minBaseline : Nat := 3
  deriving Repr

def defaultConfig : Config := {}

/-- An invoice row (`audit_invoices`). `total`/`paid` are the Python floats
`sum`/`sum_paid` in cents; `issued`/`created` are the ISO dates as day
numbers; `required` is `required_date` (nullable in the schema). -/
structure Inv where
  id : Nat
  supplier : Option Int
  number : List Char
  total : Int
  paid : Int
  status : Int
  issued : Int
  created : Int
  required : Option Int
  deriving DecidableEq, Repr

/-- A line-item row joined with its invoice (the `Row` of `_item_history`,
rules.py:161–168). Orphan items (unknown `invoice_id`) are dropped by the
join, exactly as in the code. `tax` is `tax_percent`; only its zero-ness is
ever observed by the rules, so an `Int` models it exactly. -/
structure Row where
  supplier : Option Int
  invId : Nat
  issued : Int
  number : List Char
  name : List Char
  price : Option Int
  lineSum : Option Int
  tax : Option Int
  deriving DecidableEq, Repr

/-- A `Finding` (rules.py:26–33). For the duplicate rule the code puts the
joined raw numbers in `invoice_number`; the model stores the normalized
number (the group's key) — see NOTES.md. -/
structure Finding where
  rule : Rule
  severity : Severity
  supplier : Option Int
  number : List Char
  amount : Int
  deriving DecidableEq, Repr

/-! ## `normalize_number` (rules.py:40–44)

Python: uppercase, delete every character outside `[A-Z0-9]`, then strip one
leading prefix, the regex alternation trying `INV`, then `NV`, then `IN`.
Modelled on `List Char` with ASCII-only case mapping (see NOTES.md for the
Unicode divergence). -/

def upC (c : Char) : Char :=
  if 'a' ≤ c ∧ c ≤ 'z' then Char.ofNat (c.toNat - 32) else c

def lowC (c : Char) : Char :=
  if 'A' ≤ c ∧ c ≤ 'Z' then Char.ofNat (c.toNat + 32) else c

def isKeyChar (c : Char) : Bool :=
  ('A' ≤ c ∧ c ≤ 'Z') || ('0' ≤ c ∧ c ≤ '9')

def stripPrefix (l : List Char) : List Char :=
  if ['I', 'N', 'V'].isPrefixOf l then l.drop 3
  else if ['N', 'V'].isPrefixOf l then l.drop 2
  else if ['I', 'N'].isPrefixOf l then l.drop 2
  else l

def normalize (s : List Char) : List Char :=
  stripPrefix ((s.map upC).filter isKeyChar)

theorem mem_of_drop {n : Nat} {c : Char} {l : List Char} (h : c ∈ l.drop n) : c ∈ l := by
  induction n generalizing l with
  | zero => simpa using h
  | succ n ih =>
    cases l with
    | nil => simp at h
    | cons a t => exact List.mem_cons_of_mem a (ih h)

theorem stripPrefix_subset {c : Char} {l : List Char} (h : c ∈ stripPrefix l) : c ∈ l := by
  unfold stripPrefix at h
  split at h
  · exact mem_of_drop h
  · split at h
    · exact mem_of_drop h
    · split at h
      · exact mem_of_drop h
      · exact h

/-- Every character of a normalized number is in `[A-Z0-9]`. -/
theorem normalize_chars {c : Char} {s : List Char} (h : c ∈ normalize s) : isKeyChar c = true := by
  have h' := stripPrefix_subset h
  exact (List.mem_filter.mp h').2

/-- The collisions the rule is built for (tests/test_rules.py:36–38). -/
theorem normalize_collision_inv :
    normalize "#INV20481".toList = "20481".toList ∧
    normalize "NV20481".toList = "20481".toList := by decide

theorem normalize_collision_case :
    normalize "INV-123".toList = normalize "inv123".toList := by decide

/-- The `IN` prefix also strips, so `IN100` collides with a bare `100`. -/
theorem normalize_in_prefix :
    normalize "IN100".toList = "100".toList ∧ normalize "100".toList = "100".toList := by decide

/-- Numbers that normalize to the empty string. -/
theorem normalize_empty :
    normalize "###".toList = [] ∧ normalize "INV".toList = [] := by decide

/-- `normalize` is *not* idempotent: the prefix is stripped exactly once, so
`INVINV1` normalizes to `INV1`, which would normalize further to `1`. Two
invoices numbered `INVINV1` and `INV1` therefore do *not* collide. -/
theorem normalize_not_idempotent :
    normalize "INVINV1".toList = "INV1".toList ∧
    normalize (normalize "INVINV1".toList) = "1".toList := by decide

/-! ## Dedup (first-appearance order, like a Python dict) -/

def dedupAux {α : Type} [DecidableEq α] : List α → List α → List α
  | _, [] => []
  | seen, k :: ks => if k ∈ seen then dedupAux seen ks else k :: dedupAux (k :: seen) ks

def dedup {α : Type} [DecidableEq α] (l : List α) : List α := dedupAux [] l

theorem mem_dedupAux {α : Type} [DecidableEq α] {a : α} :
    ∀ (seen : List α) (l : List α), a ∈ dedupAux seen l ↔ a ∈ l ∧ a ∉ seen := by
  intro seen l
  induction l generalizing seen with
  | nil => simp [dedupAux]
  | cons k ks ih =>
    unfold dedupAux
    split
    · rename_i h
      rw [ih]
      constructor
      · intro h'; exact ⟨List.mem_cons.mpr (Or.inr h'.1), h'.2⟩
      · intro h'
        have h1 := List.mem_cons.mp h'.1
        have hak : a ≠ k := fun e => h'.2 (e ▸ h)
        exact ⟨h1.elim (fun e => absurd e hak) id, h'.2⟩
    · rename_i h
      simp only [List.mem_cons, ih]
      constructor
      · rintro (rfl | ⟨h1, h2⟩)
        · exact ⟨Or.inl rfl, h⟩
        · exact ⟨Or.inr h1, fun hs => h2 (Or.inr hs)⟩
      · intro h'
        by_cases hak : a = k
        · exact Or.inl hak
        · have h2 : ¬(a = k ∨ a ∈ seen) := by
            intro hdis
            cases hdis with
            | inl e => exact hak e
            | inr hs => exact h'.2 hs
          exact Or.inr ⟨h'.1.elim (fun e => absurd e hak) id, h2⟩

theorem mem_dedup {α : Type} [DecidableEq α] {a : α} {l : List α} :
    a ∈ dedup l ↔ a ∈ l := by
  have h := mem_dedupAux (α := α) (a := a) [] l
  simp only [List.not_mem_nil, not_false_eq_true, and_true] at h
  exact h

theorem nodup_dedupAux {α : Type} [DecidableEq α] :
    ∀ (seen : List α) (l : List α),
      (dedupAux seen l).Nodup ∧ ∀ a ∈ dedupAux seen l, a ∉ seen := by
  intro seen l
  induction l generalizing seen with
  | nil => simp [dedupAux]
  | cons k ks ih =>
    unfold dedupAux
    split
    · exact ih seen
    · rename_i h
      have ih' := ih (seen := k :: seen)
      refine ⟨List.nodup_cons.mpr ⟨?_, ih'.1⟩, ?_⟩
      · intro hmem
        exact ih'.2 k hmem (List.mem_cons_self (a := k) (l := seen))
      · intro a ha
        have ha' := List.mem_cons.mp ha
        cases ha' with
        | inl e => subst e; exact h
        | inr hmem => exact fun hs => ih'.2 a hmem (List.mem_cons_of_mem k hs)

/-! ## Rule 1: `duplicate_numbers` (rules.py:59–81)

Invoices are grouped by `(supplier_id, normalize_number number)`; a group of
size ≥ 2 with a non-empty normalized number yields one finding whose amount
is the sum of the totals of all group members *except the first in list
order* (`group[1:]`, line 77). -/

def dupKey (inv : Inv) : Option Int × List Char := (inv.supplier, normalize inv.number)

def dupGroup (invs : List Inv) (k : Option Int × List Char) : List Inv :=
  invs.filter (fun i => decide (dupKey i = k))

theorem mem_dupGroup {i : Inv} {invs : List Inv} {k : Option Int × List Char} :
    i ∈ dupGroup invs k ↔ i ∈ invs ∧ dupKey i = k := by
  simp only [dupGroup, List.mem_filter, decide_eq_true_eq, and_comm]

def dupFindingFor (invs : List Inv) (k : Option Int × List Char) : Option Finding :=
  if 2 ≤ (dupGroup invs k).length ∧ k.2 ≠ [] then
    some { rule := .dup, severity := .high, supplier := k.1, number := k.2,
           amount := ((dupGroup invs k).tail.map Inv.total).sum }
  else none

def dupFindings (invs : List Inv) : List Finding :=
  (dedup (invs.map dupKey)).filterMap (dupFindingFor invs)

/-- Soundness/completeness: a duplicate finding exists exactly for keys that
occur in the invoice list and pass the group test. -/
theorem mem_dupFindings {f : Finding} {invs : List Inv} :
    f ∈ dupFindings invs ↔
      ∃ k, k ∈ invs.map dupKey ∧ dupFindingFor invs k = some f := by
  unfold dupFindings
  rw [List.mem_filterMap]
  constructor
  · rintro ⟨k, hk, hf⟩
    exact ⟨k, mem_dedup.mp hk, hf⟩
  · rintro ⟨k, hk, hf⟩
    exact ⟨k, mem_dedup.mpr hk, hf⟩

/-- The amount identity: finding amount + first copy's total = group total.
This is the exact sense in which the rule "conserves" the group sum. -/
theorem dup_amount_identity {invs : List Inv} {k : Option Int × List Char}
    {a : Inv} {rest : List Inv} (hg : dupGroup invs k = a :: rest)
    {f : Finding} (hf : dupFindingFor invs k = some f) :
    f.amount = (rest.map Inv.total).sum ∧
    f.amount + a.total = ((a :: rest).map Inv.total).sum := by
  unfold dupFindingFor at hf
  rw [hg] at hf
  split at hf
  · simp only [Option.some.injEq] at hf
    subst hf
    constructor
    · simp only [List.tail_cons]
    · simp only [List.tail_cons, List.map_cons, List.sum_cons]
      omega
  · simp at hf

/-- A group whose normalized number is empty is never flagged — even with
several members (the `and norm` guard, line 68). -/
theorem dup_none_of_empty_norm {invs : List Inv} {k : Option Int × List Char}
    (hk : k.2 = []) : dupFindingFor invs k = none := by
  unfold dupFindingFor
  split
  · rename_i h; exact absurd hk h.2
  · rfl

/-- A key with fewer than two invoices is never flagged. -/
theorem dup_none_of_small_group {invs : List Inv} {k : Option Int × List Char}
    (hk : (dupGroup invs k).length < 2) : dupFindingFor invs k = none := by
  unfold dupFindingFor
  split
  · rename_i h; omega
  · rfl

/-- Mirror of tests/test_rules.py (`test_duplicate_detection`). -/
theorem dup_pytest_example :
    (dupFindings [
      { id := 1, supplier := some 1, number := "INV100".toList, total := 500, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "#INV100".toList, total := 500, paid := 0,
        status := 2, issued := 4, created := 5, required := none } ]).map Finding.amount
      = [500] := by decide

/-- The same number under *different* suppliers is not a duplicate: the
grouping key includes `supplier_id` (line 63). -/
theorem dup_supplier_separation :
    dupFindings [
      { id := 1, supplier := some 1, number := "INV100".toList, total := 500, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 2, number := "INV100".toList, total := 700, paid := 0,
        status := 2, issued := 0, created := 1, required := none }] = [] := by decide

/-- **Counterexample (order dependence).** The finding amount excludes the
first group member *in list order*, so it is not a function of the multiset
of invoices: swapping the two rows changes the reported exposure from 300
to 500. (In the engine, list order is `SELECT *` order — effectively rowid
order of the SQLite table.) -/
theorem dup_order_dependence :
    (dupFindings [
      { id := 1, supplier := some 1, number := "X".toList, total := 500, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "X".toList, total := 300, paid := 0,
        status := 2, issued := 0, created := 1, required := none } ]).map Finding.amount = [300] ∧
    (dupFindings [
      { id := 2, supplier := some 1, number := "X".toList, total := 300, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 1, supplier := some 1, number := "X".toList, total := 500, paid := 0,
        status := 2, issued := 0, created := 1, required := none } ]).map Finding.amount = [500] := by
  decide

/-! ## Rule 2: `entry_lag` (rules.py:84–104)

Flag iff `created - issued > entry_lag_days` (strict). Severity: `med` when
`lag ≤ 45`, else `high` (line 94). Invoices whose dates fail to parse are
skipped by the code's `try/except`; the model takes dates as day numbers,
so that path is not represented (see NOTES.md). -/

def lagOf (inv : Inv) : Int := inv.created - inv.issued

def lagFinding (cfg : Config) (inv : Inv) : Option Finding :=
  if lagOf inv > cfg.entryLagDays then
    some { rule := .lag, severity := if lagOf inv ≤ 45 then .med else .high,
           supplier := inv.supplier, number := inv.number, amount := inv.total }
  else none

def lagFindings (cfg : Config) (invs : List Inv) : List Finding :=
  invs.filterMap (lagFinding cfg)

/-- Exact characterization of the entry-lag finding. -/
theorem lagFinding_eq_some {cfg : Config} {inv : Inv} {f : Finding} :
    lagFinding cfg inv = some f ↔
      cfg.entryLagDays < lagOf inv ∧
      f = { rule := .lag, severity := if lagOf inv ≤ 45 then .med else .high,
            supplier := inv.supplier, number := inv.number, amount := inv.total } := by
  unfold lagFinding
  split
  · rename_i h
    simp only [Option.some.injEq]
    constructor
    · intro e; exact ⟨h, e.symm⟩
    · intro hcon; exact hcon.2.symm
  · rename_i h
    constructor
    · intro hf; cases hf
    · intro hcon; have h2 := hcon.1; omega

/-- Boundary: a lag of exactly `entry_lag_days` is *not* flagged (strict >). -/
theorem lag_not_flagged_at_threshold {cfg : Config} {inv : Inv}
    (h : lagOf inv = cfg.entryLagDays) : lagFinding cfg inv = none := by
  unfold lagFinding
  split
  · rename_i hc; omega
  · rfl

/-- Severity and flagging boundaries, on concrete invoices (default config):
lag 14 → not flagged; lag 15 → flagged `med`; lag 45 → `med`; lag 46 → `high`. -/
theorem lag_boundaries :
    lagFinding defaultConfig
      { id := 1, supplier := some 1, number := "A".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 14, required := none } = none ∧
    (lagFinding defaultConfig
      { id := 1, supplier := some 1, number := "A".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 15, required := none }).map Finding.severity
      = some .med ∧
    (lagFinding defaultConfig
      { id := 1, supplier := some 1, number := "A".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 45, required := none }).map Finding.severity
      = some .med ∧
    (lagFinding defaultConfig
      { id := 1, supplier := some 1, number := "A".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 46, required := none }).map Finding.severity
      = some .high := by decide

/-! ## Rule 3: `overdue_unpaid` (rules.py:107–134)

Skip if `sum_paid` is truthy (nonzero) and `sum_paid >= sum` (line 111);
require status ∈ {2, 4} (approved / partly paid, line 113); flag iff
`today - due > overdue_days` (strict), where `due = required_date or
issue_date` (line 117). Severity `high` iff `days_over > 60`. Amount is
`sum - sum_paid`. The code uses `datetime.now()`; the model takes `today`
as a parameter. -/

def dueOf (inv : Inv) : Int := inv.required.getD inv.issued

def overdueFinding (cfg : Config) (today : Int) (inv : Inv) : Option Finding :=
  if inv.paid ≠ 0 ∧ inv.total ≤ inv.paid then none
  else if inv.status ≠ 2 ∧ inv.status ≠ 4 then none
  else if today - dueOf inv > cfg.overdueDays then
    some { rule := .overdue, severity := if today - dueOf inv > 60 then .high else .med,
           supplier := inv.supplier, number := inv.number, amount := inv.total - inv.paid }
  else none

def overdueFindings (cfg : Config) (today : Int) (invs : List Inv) : List Finding :=
  invs.filterMap (overdueFinding cfg today)

/-- Exact characterization of the overdue finding. -/
theorem overdueFinding_eq_some {cfg : Config} {today : Int} {inv : Inv} {f : Finding} :
    overdueFinding cfg today inv = some f ↔
      ¬ (inv.paid ≠ 0 ∧ inv.total ≤ inv.paid) ∧
      (inv.status = 2 ∨ inv.status = 4) ∧
      cfg.overdueDays < today - dueOf inv ∧
      f = { rule := .overdue, severity := if today - dueOf inv > 60 then .high else .med,
            supplier := inv.supplier, number := inv.number, amount := inv.total - inv.paid } := by
  by_cases h1 : inv.paid ≠ 0 ∧ inv.total ≤ inv.paid
  · unfold overdueFinding
    rw [if_pos h1]
    constructor
    · intro hf; cases hf
    · intro hcon; exact (hcon.1 h1).elim
  · by_cases h2 : inv.status = 2 ∨ inv.status = 4
    · have hstat : ¬ (inv.status ≠ 2 ∧ inv.status ≠ 4) := by
        intro hc
        cases h2 with
        | inl e => exact hc.1 e
        | inr e => exact hc.2 e
      by_cases h3 : today - dueOf inv > cfg.overdueDays
      · unfold overdueFinding
        rw [if_neg h1, if_neg hstat, if_pos h3]
        simp only [Option.some.injEq]
        constructor
        · intro e; exact ⟨h1, h2, h3, e.symm⟩
        · intro hcon; exact hcon.2.2.2.symm
      · unfold overdueFinding
        rw [if_neg h1, if_neg hstat, if_neg h3]
        constructor
        · intro hf; cases hf
        · intro hcon; exact absurd hcon.2.2.1 h3
    · have hstat : inv.status ≠ 2 ∧ inv.status ≠ 4 :=
        ⟨fun e => h2 (Or.inl e), fun e => h2 (Or.inr e)⟩
      unfold overdueFinding
      rw [if_neg h1, if_pos hstat]
      constructor
      · intro hf; cases hf
      · intro hcon; exact absurd hcon.2.1 h2

/-- A fully-paid invoice (with a nonzero payment) is never overdue. -/
theorem overdue_none_of_paid {cfg : Config} {today : Int} {inv : Inv}
    (h0 : inv.paid ≠ 0) (h1 : inv.total ≤ inv.paid) :
    overdueFinding cfg today inv = none := by
  unfold overdueFinding
  rw [if_pos ⟨h0, h1⟩]

/-- Boundaries on concrete invoices (default config, `today = 100`, due day 0,
status 2, unpaid): 10 days over → not flagged; 11 → `med`; 60 → `med`;
61 → `high`. Amounts are the unpaid balance. -/
theorem overdue_boundaries :
    overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := 1000, paid := 0,
        status := 2, issued := 90, created := 90, required := none } = none ∧
    (overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := 1000, paid := 250,
        status := 2, issued := 89, created := 89, required := none }).map
        (fun f => (f.severity, f.amount)) = some (.med, 750) ∧
    (overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := 1000, paid := 0,
        status := 4, issued := 40, created := 40, required := none }).map Finding.severity
      = some .med ∧
    (overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := 1000, paid := 0,
        status := 2, issued := 39, created := 39, required := none }).map Finding.severity
      = some .high := by decide

/-- Edge case: a zero-total, zero-paid invoice is *not* skipped (0 is falsy
in Python, line 111), so an old zero invoice with status 2 is flagged with
amount 0. A negative-total (credit) invoice is flagged with a *negative*
amount. -/
theorem overdue_zero_and_negative_edges :
    (overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := 0, paid := 0,
        status := 2, issued := 0, created := 0, required := none }).map Finding.amount
      = some 0 ∧
    (overdueFinding defaultConfig 100
      { id := 1, supplier := some 1, number := "A".toList, total := -5000, paid := 0,
        status := 2, issued := 0, created := 0, required := none }).map Finding.amount
      = some (-5000) := by decide

/-! ## Rule 4: `amount_outliers` (rules.py:137–158)

Per supplier group: groups with fewer than `min_invoices_for_baseline + 1`
invoices are skipped; the median of the group's totals is the baseline
(outliers included); a group whose median is ≤ 0 is skipped; an invoice is
flagged iff `sum > median * amount_outlier_multiple` (strict). The model
works with `twiceMedian` (twice the median, an integer) so the comparison
`2 * total > twiceMedian * multiple` is exact. -/

def insertI (x : Int) : List Int → List Int
  | [] => [x]
  | y :: ys => if x ≤ y then x :: y :: ys else y :: insertI x ys

def isort : List Int → List Int
  | [] => []
  | x :: xs => insertI x (isort xs)

theorem insertI_perm (x : Int) (l : List Int) : List.Perm (insertI x l) (x :: l) := by
  induction l with
  | nil => exact List.Perm.refl [x]
  | cons y ys ih =>
    unfold insertI
    split
    · exact List.Perm.refl _
    · exact (List.Perm.cons y ih).trans (List.Perm.swap x y ys)

theorem isort_perm (l : List Int) : List.Perm (isort l) l := by
  induction l with
  | nil => exact List.Perm.refl []
  | cons x xs ih => exact (insertI_perm x (isort xs)).trans (List.Perm.cons x ih)

theorem insertI_sorted {x : Int} {l : List Int}
    (hs : l.Pairwise (· ≤ ·)) : (insertI x l).Pairwise (· ≤ ·) := by
  induction l with
  | nil =>
    exact List.Pairwise.cons (fun b hb => by simp at hb) List.Pairwise.nil
  | cons y ys ih =>
    have hs' := List.pairwise_cons.mp hs
    unfold insertI
    split
    · rename_i h
      refine List.Pairwise.cons ?_ hs
      intro a ha
      cases List.mem_cons.mp ha with
      | inl e => subst e; exact h
      | inr hm => exact Int.le_trans h (hs'.1 a hm)
    · rename_i h
      have hyx : y ≤ x := by omega
      refine List.Pairwise.cons ?_ (ih hs'.2)
      intro a ha
      have hmem : a ∈ x :: ys := (insertI_perm x ys).mem_iff.mp ha
      cases List.mem_cons.mp hmem with
      | inl e => subst e; exact hyx
      | inr hm => exact hs'.1 a hm

theorem isort_sorted (l : List Int) : (isort l).Pairwise (· ≤ ·) := by
  induction l with
  | nil => exact List.Pairwise.nil
  | cons x xs ih => exact insertI_sorted ih

/-- Twice the median of a *sorted* list (0 for the empty list). -/
def twiceMedian (sorted : List Int) : Int :=
  let n := sorted.length
  if n % 2 = 1 then 2 * sorted.getD (n / 2) 0
  else sorted.getD (n / 2 - 1) 0 + sorted.getD (n / 2) 0

def supplierGroup (invs : List Inv) (s : Option Int) : List Inv :=
  invs.filter (fun i => decide (i.supplier = s))

def outlierFinding (cfg : Config) (group : List Inv) (inv : Inv) : Option Finding :=
  if group.length < cfg.minBaseline + 1 ∨
      twiceMedian (isort (group.map Inv.total)) ≤ 0 then none
  else if 2 * inv.total > twiceMedian (isort (group.map Inv.total)) * cfg.outlierMultiple then
    some { rule := .outlier, severity := .med, supplier := inv.supplier,
           number := inv.number, amount := inv.total }
  else none

def outlierFindings (cfg : Config) (invs : List Inv) : List Finding :=
  (dedup (invs.map Inv.supplier)).flatMap
    (fun s => (supplierGroup invs s).filterMap (outlierFinding cfg (supplierGroup invs s)))

/-- Exact characterization of the outlier predicate on a group. The middle
conjuncts are the two skip conditions of lines 146 and 149; the comparison
is the strict line-152 test in cross-multiplied (exact) form, and it is
equivalent to `inv.total > median * multiple` over the rationals. -/
theorem outlierFinding_eq_some {cfg : Config} {group : List Inv} {inv : Inv} {f : Finding} :
    outlierFinding cfg group inv = some f ↔
      cfg.minBaseline + 1 ≤ group.length ∧
      0 < twiceMedian (isort (group.map Inv.total)) ∧
      twiceMedian (isort (group.map Inv.total)) * cfg.outlierMultiple < 2 * inv.total ∧
      f = { rule := .outlier, severity := .med, supplier := inv.supplier,
            number := inv.number, amount := inv.total } := by
  unfold outlierFinding
  split
  · rename_i h
    constructor
    · intro hf; cases hf
    · intro hcon
      cases h with
      | inl hl => have := hcon.1; omega
      | inr hm => have := hcon.2.1; omega
  · split
    · rename_i h1 h2
      simp only [Option.some.injEq]
      constructor
      · intro e
        have hnA : ¬ group.length < cfg.minBaseline + 1 := fun ha => h1 (Or.inl ha)
        have hnB : ¬ twiceMedian (isort (group.map Inv.total)) ≤ 0 := fun hb => h1 (Or.inr hb)
        exact ⟨by omega, by omega, h2, e.symm⟩
      · intro hcon; exact hcon.2.2.2.symm
    · rename_i h1 h2
      constructor
      · intro hf; cases hf
      · intro hcon; exact absurd hcon.2.2.1 h2

/-- Boundary: an invoice at exactly `multiple × median` is *not* flagged. -/
theorem outlier_boundary_exact_multiple :
    outlierFindings defaultConfig [
      { id := 1, supplier := some 1, number := "A1".toList, total := 2000, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "A2".toList, total := 2000, paid := 0,
        status := 2, issued := 30, created := 31, required := none },
      { id := 3, supplier := some 1, number := "A3".toList, total := 2000, paid := 0,
        status := 2, issued := 60, created := 61, required := none },
      { id := 4, supplier := some 1, number := "A4".toList, total := 2000, paid := 0,
        status := 2, issued := 90, created := 91, required := none },
      { id := 5, supplier := some 1, number := "A5".toList, total := 6000, paid := 0,
        status := 2, issued := 120, created := 121, required := none }] = [] := by decide

/-- Mirror of tests/test_rules.py (`test_amount_outlier`): totals of 2000 ×4
plus 9000 (median 2000, threshold 6000) flag only the 9000 invoice. -/
theorem outlier_pytest_example :
    (outlierFindings defaultConfig [
      { id := 1, supplier := some 1, number := "A1".toList, total := 2000, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "A2".toList, total := 2000, paid := 0,
        status := 2, issued := 30, created := 31, required := none },
      { id := 3, supplier := some 1, number := "A3".toList, total := 2000, paid := 0,
        status := 2, issued := 60, created := 61, required := none },
      { id := 4, supplier := some 1, number := "A4".toList, total := 2000, paid := 0,
        status := 2, issued := 90, created := 91, required := none },
      { id := 9, supplier := some 1, number := "A9".toList, total := 9000, paid := 0,
        status := 2, issued := 120, created := 121, required := none }]).map Finding.number
      = ["A9".toList] := by decide

/-- Median masking: when most invoices are large, the median is large and
*nothing* is flagged — the baseline is not leave-one-out and not robust to
a majority of outliers. -/
theorem outlier_median_masking :
    outlierFindings defaultConfig [
      { id := 1, supplier := some 1, number := "A1".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "A2".toList, total := 10000, paid := 0,
        status := 2, issued := 30, created := 31, required := none },
      { id := 3, supplier := some 1, number := "A3".toList, total := 10000, paid := 0,
        status := 2, issued := 60, created := 61, required := none },
      { id := 4, supplier := some 1, number := "A4".toList, total := 10000, paid := 0,
        status := 2, issued := 90, created := 91, required := none }] = [] := by decide

/-- A group of `minBaseline` invoices (one short of the gate) is never
examined, however extreme the amounts. -/
theorem outlier_small_group :
    outlierFindings defaultConfig [
      { id := 1, supplier := some 1, number := "A1".toList, total := 100, paid := 0,
        status := 2, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "A2".toList, total := 100, paid := 0,
        status := 2, issued := 30, created := 31, required := none },
      { id := 3, supplier := some 1, number := "A3".toList, total := 999999, paid := 0,
        status := 2, issued := 60, created := 61, required := none }] = [] := by decide

/-! ## Item history (rules.py:161–168)

`_item_history` joins items to invoices (items whose invoice is missing
are dropped) and sorts by the invoice's `issue_date` — a *string* sort of
the raw stored value (stable, so ties keep input order). The model takes
the history as already built: a list of `Row`s in history order. The code
lowercases item names for keying (`name.lower()`); the model stores
`Row.name` already lowercased. -/

/-- Lookup in a history, mirroring `hist[key][0]` = the first row for a key. -/
def baselineFor (hist : List Row) (s : Option Int) (nm : List Char) : Option Row :=
  hist.find? (fun r => r.supplier = s && r.name = nm)

/-- The baseline row is a member of the history and matches the key.
`find?` returns the *first* match in list order, which is exactly the
code's `hist[key][0]` (rules.py:176). -/
theorem baselineFor_spec {hist : List Row} {s : Option Int} {nm : List Char} {b : Row}
    (h : baselineFor hist s nm = some b) :
    b ∈ hist ∧ b.supplier = s ∧ b.name = nm := by
  have hmem := List.mem_of_find?_eq_some h
  have hpred := List.find?_some h
  simp only [Bool.and_eq_true, decide_eq_true_eq] at hpred
  exact ⟨hmem, hpred.1, hpred.2⟩

/-! ## Rule 5: `rate_changes` (rules.py:171–198)

For each `(supplier, item-name)` key, the baseline is the price of the
*first* history row; it never updates. Rows with `price is None` or
`price < 0` are excluded from the history entirely (line 174). A row is
flagged iff the baseline is nonzero (`if not baseline: continue`) and
`100 * |price - baseline| / baseline > rate_change_pct` (strict); the model
uses the cross-multiplied form `100 * |price - baseline| > pct * baseline`,
exact for integers whenever the baseline is positive, which the code
guarantees on the flagging path. A *negative* baseline is impossible here:
negative-price rows never enter the history. -/

def rateHistory (rows : List Row) : List Row :=
  rows.filter (fun r => match r.price with | some p => decide (0 ≤ p) | none => false)

def rateFinding (cfg : Config) (hist : List Row) (r : Row) : Option Finding :=
  r.price.bind (fun p =>
    (baselineFor hist r.supplier r.name).bind (fun base =>
      base.price.bind (fun bp =>
        if bp ≠ 0 ∧ cfg.rateChangePct * bp < 100 * (p - bp).natAbs then
          some { rule := .rate, severity := .high, supplier := r.supplier,
                 number := r.number, amount := r.lineSum.getD 0 }
        else none)))

def rateFindings (cfg : Config) (rows : List Row) : List Finding :=
  (rateHistory rows).filterMap (rateFinding cfg (rateHistory rows))

/-- Exact characterization of the rate-change finding, in terms of the
first history row for the row's key. -/
theorem rateFinding_eq_some {cfg : Config} {hist : List Row} {r : Row} {f : Finding} :
    rateFinding cfg hist r = some f ↔
      ∃ (p bp : Int), r.price = some p ∧
        (∃ base, baselineFor hist r.supplier r.name = some base ∧ base.price = some bp) ∧
        bp ≠ 0 ∧ cfg.rateChangePct * bp < 100 * (p - bp).natAbs ∧
        f = { rule := .rate, severity := .high, supplier := r.supplier,
              number := r.number, amount := r.lineSum.getD 0 } := by
  constructor
  · intro h
    unfold rateFinding at h
    rw [Option.bind_eq_some_iff] at h
    obtain ⟨p, hrp, h⟩ := h
    rw [Option.bind_eq_some_iff] at h
    obtain ⟨base, hb, h⟩ := h
    rw [Option.bind_eq_some_iff] at h
    obtain ⟨bp, hbp, h⟩ := h
    split at h
    · rename_i hc
      simp only [Option.some.injEq] at h
      exact ⟨p, bp, hrp, ⟨base, hb, hbp⟩, hc.1, hc.2, h.symm⟩
    · cases h
  · rintro ⟨p, bp, hrp, ⟨base, hb, hbp⟩, hb0, hgt, rfl⟩
    unfold rateFinding
    rw [hrp, Option.bind_some, hb, Option.bind_some, hbp, Option.bind_some,
      if_pos ⟨hb0, hgt⟩]

/-- Boundary: a change of exactly `rate_change_pct` (5%) is *not* flagged
(strict >); 6% is. -/
theorem rate_boundary :
    (rateFindings defaultConfig [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "labor".toList, price := some 10000, lineSum := some 10000, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 30,
        name := "labor".toList, price := some 10500, lineSum := some 10500, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 60,
        name := "labor".toList, price := some 10600, lineSum := some 10600, tax := none }]
      ).map (fun f => f.amount) = [10600] := by decide

/-- A zero first price permanently disables the rule for that key: the
baseline stays 0 (`if not baseline: continue`), so even a later jump to a
large price is never flagged. -/
theorem rate_zero_baseline_disables :
    rateFindings defaultConfig [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "labor".toList, price := some 0, lineSum := some 0, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 30,
        name := "labor".toList, price := some 50000, lineSum := some 50000, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 60,
        name := "labor".toList, price := some 90000, lineSum := some 90000, tax := none }]
      = [] := by decide

/-- The baseline never updates: after a +100% jump (flagged), a return to
only +4% over the *original* price is not flagged, even though it is a
−48% move from the previous invoice. -/
theorem rate_baseline_never_updates :
    (rateFindings defaultConfig [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "labor".toList, price := some 10000, lineSum := some 10000, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 30,
        name := "labor".toList, price := some 20000, lineSum := some 20000, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 60,
        name := "labor".toList, price := some 10400, lineSum := some 10400, tax := none }]
      ).map (fun f => f.amount) = [20000] := by decide

/-! ## Rule 6: `new_charge_types` (rules.py:199–230)

Walking the history in order, the code counts, per supplier, how many
*item-bearing* invoices have been seen: the counter increments whenever
the current row's invoice number differs from the previous row's
(line 210–214) — note it compares invoice-number *strings*, not invoice
ids, and invoices with no items never appear in the history at all. A
first-seen item name is flagged iff the counter is already > 3, i.e. from
the supplier's 5th item-bearing invoice onwards in practice. Amount is
`line_sum or price or 0` — Python `or`, so a `line_sum` of 0 falls through
to the price (lines 225–226). -/

/-- Per-supplier loop state for `new_charge_types`: association list
mapping a supplier to (invoice counter, last invoice number seen). -/
abbrev CntState := List (Option Int × Nat × Option (List Char))

def cntLookup (st : CntState) (s : Option Int) : Nat × Option (List Char) :=
  match st.find? (fun p => decide (p.1 = s)) with
  | some p => (p.2.1, p.2.2)
  | none => (0, none)

def cntUpdate : CntState → Option Int → Nat → Option (List Char) → CntState
  | [], s, c, last => [(s, c, last)]
  | p :: ps, s, c, last =>
    if p.1 = s then (s, c, last) :: ps else p :: cntUpdate ps s c last

/-- Per-supplier seen-name state: association list supplier → names seen. -/
abbrev SeenState := List (Option Int × List (List Char))

def seenLookup (st : SeenState) (s : Option Int) : List (List Char) :=
  ((st.find? (fun p => decide (p.1 = s))).map Prod.snd).getD []

def seenUpdate : SeenState → Option Int → List (List Char) → SeenState
  | [], s, ns => [(s, ns)]
  | p :: ps, s, ns =>
    if p.1 = s then (s, ns) :: ps else p :: seenUpdate ps s ns

/-- The `line_sum or price or 0` amount of rules.py:225: Python `or`
treats 0 as absent, so a zero `line_sum` falls through to the price. -/
def orAmount (r : Row) : Int :=
  match r.lineSum with
  | some v => if v ≠ 0 then v else r.price.getD 0
  | none => r.price.getD 0

/-- The counter after processing a row (rules.py:213–215): incremented
iff the row's invoice number differs from the supplier's previous one. -/
def nctCount' (counts : CntState) (r : Row) : Nat :=
  let (cnt, last) := cntLookup counts r.supplier
  if some r.number ≠ last then cnt + 1 else cnt

def nctKnown (seen : SeenState) (r : Row) : Bool :=
  (seenLookup seen r.supplier).any (fun n => decide (n = r.name))

def nctStepFinding (counts : CntState) (seen : SeenState) (r : Row) : Option Finding :=
  if nctKnown seen r = false ∧ 3 < nctCount' counts r then
    some { rule := .newCharge, severity := .med, supplier := r.supplier,
           number := r.number, amount := orAmount r }
  else none

def nctNextCounts (counts : CntState) (r : Row) : CntState :=
  cntUpdate counts r.supplier (nctCount' counts r) (some r.number)

def nctNextSeen (seen : SeenState) (r : Row) : SeenState :=
  seenUpdate seen r.supplier
    (if nctKnown seen r then seenLookup seen r.supplier
     else seenLookup seen r.supplier ++ [r.name])

/-- The `new_charge_types` pass (rules.py:211–229) as a recursion over
the history carrying the code's two dicts as state. -/
def nctRec : List Row → CntState → SeenState → List Finding
  | [], _, _ => []
  | r :: rs, counts, seen =>
    (nctStepFinding counts seen r).toList ++
      nctRec rs (nctNextCounts counts r) (nctNextSeen seen r)

def newChargeFindings (rows : List Row) : List Finding :=
  nctRec rows [] []

/-- Step characterization: a finding is emitted at a row iff the row's
item name is first-seen for its supplier *and* the row's (incremented)
invoice counter exceeds 3. -/
theorem nctStepFinding_eq_some {counts : CntState} {seen : SeenState}
    {r : Row} {f : Finding} :
    nctStepFinding counts seen r = some f ↔
      nctKnown seen r = false ∧ 3 < nctCount' counts r ∧
      f = { rule := .newCharge, severity := .med, supplier := r.supplier,
            number := r.number, amount := orAmount r } := by
  unfold nctStepFinding
  split
  · rename_i hc
    simp only [Option.some.injEq]
    exact ⟨fun e => ⟨hc.1, hc.2, e.symm⟩, fun ⟨_, _, e⟩ => e.symm⟩
  · rename_i hc
    constructor
    · intro hf; cases hf
    · intro hcon; exact (hc ⟨hcon.1, hcon.2.1⟩).elim

/-- Soundness for the loop: every emitted finding is a `new_charge_type`
finding of severity `med` whose supplier, number and amount come from a
single history row (with the Python-`or` amount). -/
theorem nctRec_sound : ∀ (rows : List Row) (counts : CntState) (seen : SeenState)
    (f : Finding), f ∈ nctRec rows counts seen →
    f.rule = .newCharge ∧ f.severity = .med ∧
    ∃ r ∈ rows, f.supplier = r.supplier ∧ f.number = r.number ∧
      f.amount = orAmount r := by
  intro rows
  induction rows with
  | nil => intro _ _ _ hf; simp [nctRec] at hf
  | cons r rs ih =>
    intro counts seen f hf
    simp only [nctRec, List.mem_append, Option.mem_toList] at hf
    cases hf with
    | inl hf =>
      have hf' : nctStepFinding counts seen r = some f := hf
      rw [nctStepFinding_eq_some] at hf'
      obtain ⟨_, _, rfl⟩ := hf'
      exact ⟨rfl, rfl, r, List.mem_cons_self (a := r) (l := rs), rfl, rfl, rfl⟩
    | inr hf =>
      obtain ⟨h1, h2, r', hr', h3, h4, h5⟩ := ih _ _ _ hf
      exact ⟨h1, h2, r', List.mem_cons_of_mem r hr', h3, h4, h5⟩

/-- Boundary: a first-seen charge type on the supplier's 3rd distinct
invoice is *not* flagged; the same novelty on the 4th is (counter > 3). -/
theorem nct_boundary :
    newChargeFindings [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 10,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 20,
        name := "rush fee".toList, price := some 50, lineSum := some 50, tax := none }] = [] ∧
    (newChargeFindings [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 10,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 20,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 4, supplier := some 1, number := "N4".toList, issued := 30,
        name := "rush fee".toList, price := some 50, lineSum := some 50, tax := none }]).map
        Finding.number = ["N4".toList] := by decide

/-- The counter counts invoice-number *changes*, not invoices: the number
sequence A, B, A counts 3, so a novelty on the next new number (count 4,
on only the 4th row) is already flagged — a supplier whose numbers
repeat non-consecutively reaches the threshold sooner than the
docstring's "after their first three invoices" suggests. -/
theorem nct_recount_on_repeated_number :
    (newChargeFindings [
      { invId := 1, supplier := some 1, number := "A".toList, issued := 0,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 2, supplier := some 1, number := "B".toList, issued := 10,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 1, supplier := some 1, number := "A".toList, issued := 20,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 3, supplier := some 1, number := "C".toList, issued := 30,
        name := "rush fee".toList, price := some 50, lineSum := some 50, tax := none }]).map
        Finding.number = ["C".toList] := by decide

/-- Amount fallback: a first-seen charge with `line_sum = 0` reports the
*price*, because Python's `or` treats 0 as missing (rules.py:225). -/
theorem nct_zero_line_sum_falls_through :
    (newChargeFindings [
      { invId := 1, supplier := some 1, number := "N1".toList, issued := 0,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 2, supplier := some 1, number := "N2".toList, issued := 10,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 3, supplier := some 1, number := "N3".toList, issued := 20,
        name := "base".toList, price := some 100, lineSum := some 100, tax := none },
      { invId := 4, supplier := some 1, number := "N4".toList, issued := 30,
        name := "rush fee".toList, price := some 777, lineSum := some 0, tax := none }]).map
        Finding.amount = [777] := by decide

/-! ## Rule 7: `negative_adjustments` (rules.py:236–256)

Iterates the *raw* item list (not the date-sorted history): every item
with a negative price whose invoice exists yields one `unexplained_credit`
finding whose amount is the (negative) price itself. Note the symmetry
with rule 5: negative-price rows are excluded from the rate history —
here they are the *only* rows that produce findings. An item whose
invoice is missing is silently skipped. The model takes rows already
joined to their invoices (missing-invoice items dropped, as in `Row`). -/

def creditFinding (r : Row) : Option Finding :=
  r.price.bind (fun p =>
    if p < 0 then
      some { rule := .credit, severity := .low, supplier := r.supplier,
             number := r.number, amount := p }
    else none)

def creditFindings (rows : List Row) : List Finding :=
  rows.filterMap creditFinding

/-- Exact characterization: a credit finding exists for a row iff the row
carries a negative price, and then it is uniquely determined. -/
theorem creditFinding_eq_some {r : Row} {f : Finding} :
    creditFinding r = some f ↔
      ∃ p : Int, r.price = some p ∧ p < 0 ∧
        f = { rule := .credit, severity := .low, supplier := r.supplier,
              number := r.number, amount := p } := by
  constructor
  · intro h
    unfold creditFinding at h
    rw [Option.bind_eq_some_iff] at h
    obtain ⟨p, hp, h⟩ := h
    split at h
    · rename_i hlt
      simp only [Option.some.injEq] at h
      exact ⟨p, hp, hlt, h.symm⟩
    · cases h
  · rintro ⟨p, hp, hlt, rfl⟩
    unfold creditFinding
    rw [hp, Option.bind_some, if_pos hlt]

/-- Membership form: `f ∈ creditFindings rows` iff `f` is the credit
finding of a negative-price row of `rows`. -/
theorem mem_creditFindings {rows : List Row} {f : Finding} :
    f ∈ creditFindings rows ↔
      ∃ r ∈ rows, ∃ p : Int, r.price = some p ∧ p < 0 ∧
        f = { rule := .credit, severity := .low, supplier := r.supplier,
              number := r.number, amount := p } := by
  unfold creditFindings
  rw [List.mem_filterMap]
  constructor
  · rintro ⟨r, hr, hf⟩
    rw [creditFinding_eq_some] at hf
    obtain ⟨p, hp, hlt, rfl⟩ := hf
    exact ⟨r, hr, p, hp, hlt, rfl⟩
  · rintro ⟨r, hr, p, hp, hlt, rfl⟩
    exact ⟨r, hr, by rw [creditFinding_eq_some]; exact ⟨p, hp, hlt, rfl⟩⟩

/-! ## Rule 8: `inconsistent_tax` (rules.py:259–276)

For each `(supplier, item)` key the code collects the set of
`bool(tax_percent)` over the *whole* history (None and 0 both map to
`False`; any nonzero — including a *negative* rate — maps to `True`).
Keys with both values are "flagged"; scanning the history again, the
finding is emitted on the first row of a flagged key whose tax is
truthy, exactly one per key (the key is then discarded), with amount
`line_sum or 0`. The model records only the zero-ness of `tax_percent`,
which is all the rule observes. Note the mixed predicate is computed
over the full history, not the remaining suffix. -/

def taxFlag (r : Row) : Bool :=
  match r.tax with
  | some t => decide (t ≠ 0)
  | none => false

def taxKeyMixed (rows : List Row) (s : Option Int) (nm : List Char) : Bool :=
  (rows.any (fun r => decide (r.supplier = s ∧ r.name = nm) && taxFlag r)) &&
  (rows.any (fun r => decide (r.supplier = s ∧ r.name = nm) && !taxFlag r))

/-- The reporting scan: recursion over the history carrying the keys
already reported (the code's `flagged.discard`, line 274). -/
def taxRec (all : List Row) : List Row → List (Option Int × List Char) → List Finding
  | [], _ => []
  | r :: rs, done =>
    if taxKeyMixed all r.supplier r.name = true ∧ taxFlag r = true ∧
        (r.supplier, r.name) ∉ done then
      { rule := .tax, severity := .med, supplier := r.supplier,
        number := r.number, amount := r.lineSum.getD 0 } ::
        taxRec all rs ((r.supplier, r.name) :: done)
    else taxRec all rs done

def taxFindings (rows : List Row) : List Finding :=
  taxRec rows rows []

/-- Soundness: every tax finding is emitted at a *taxed* row whose key is
mixed over the full history, with amount `line_sum or 0`. -/
theorem taxRec_sound : ∀ (all rows : List Row) (done : List (Option Int × List Char))
    (f : Finding), f ∈ taxRec all rows done →
    f.rule = .tax ∧ f.severity = .med ∧
    ∃ r ∈ rows, taxFlag r = true ∧ taxKeyMixed all r.supplier r.name = true ∧
      f.supplier = r.supplier ∧ f.number = r.number ∧
      f.amount = r.lineSum.getD 0 := by
  intro all rows
  induction rows with
  | nil => intro _ _ hf; simp [taxRec] at hf
  | cons r rs ih =>
    intro done f hf
    simp only [taxRec] at hf
    split at hf
    · rename_i hc
      rw [List.mem_cons] at hf
      cases hf with
      | inl e =>
        subst e
        exact ⟨rfl, rfl, r, List.mem_cons_self (a := r) (l := rs), hc.2.1, hc.1, rfl, rfl, rfl⟩
      | inr hf =>
        obtain ⟨h1, h2, r', hr', h3, h4, h5, h6, h7⟩ := ih _ _ hf
        exact ⟨h1, h2, r', List.mem_cons_of_mem r hr', h3, h4, h5, h6, h7⟩
    · obtain ⟨h1, h2, r', hr', h3, h4, h5, h6, h7⟩ := ih _ _ hf
      exact ⟨h1, h2, r', List.mem_cons_of_mem r hr', h3, h4, h5, h6, h7⟩

/-- Concrete scenarios: (1) tax present vs missing yields exactly one
finding, on the *taxed* row, amount = its line sum — even when the
untaxed row comes first; (2) consistently taxed rows yield none;
(3) two taxed rows for one mixed key still yield exactly one finding
(one per key, on the first taxed row). -/
theorem tax_scenarios :
    (taxFindings [
      { invId := 1, supplier := some 1, number := "T1".toList, issued := 0,
        name := "widget".toList, price := some 1000, lineSum := some 1000, tax := none },
      { invId := 2, supplier := some 1, number := "T2".toList, issued := 10,
        name := "widget".toList, price := some 1000, lineSum := some 1234,
        tax := some 8625 }]).map (fun f => (f.number, f.amount))
      = [("T2".toList, 1234)] ∧
    (taxFindings [
      { invId := 1, supplier := some 1, number := "W1".toList, issued := 0,
        name := "widget".toList, price := some 1000, lineSum := some 1000,
        tax := some 500 },
      { invId := 2, supplier := some 1, number := "W2".toList, issued := 10,
        name := "widget".toList, price := some 1000, lineSum := some 1000,
        tax := some 700 }]) = [] ∧
    (taxFindings [
      { invId := 1, supplier := some 1, number := "X1".toList, issued := 0,
        name := "widget".toList, price := some 1000, lineSum := some 100,
        tax := some 500 },
      { invId := 2, supplier := some 1, number := "X2".toList, issued := 10,
        name := "widget".toList, price := some 1000, lineSum := some 200, tax := none },
      { invId := 3, supplier := some 1, number := "X3".toList, issued := 20,
        name := "widget".toList, price := some 1000, lineSum := some 300,
        tax := some 700 }]).map (fun f => (f.number, f.amount))
      = [("X1".toList, 100)] := by decide

/-! ## Aggregation: `run_all` (rules.py:46–57) and the dashboard total

`run_all` concatenates the eight rules' outputs in the fixed order
below, with no cross-rule deduplication: one invoice can appear in
several rules' findings. The item-based rules split into two orderings
in the code — rate/new-charge/tax consume the date-sorted history,
negative-adjustments consumes the raw item order — so the model takes
both row lists as inputs.

The dashboard KPI (web.py:49) is
`total_flagged = sum(abs(f.amount or 0) for f in findings)` — a sum over
*findings*, not over invoices. -/

def runAll (cfg : Config) (today : Int) (invs : List Inv)
    (histRows rawRows : List Row) : List Finding :=
  dupFindings invs ++ lagFindings cfg invs ++ overdueFindings cfg today invs ++
  outlierFindings cfg invs ++ rateFindings cfg histRows ++
  newChargeFindings histRows ++ creditFindings rawRows ++ taxFindings histRows

/-- Completeness/soundness of the pipeline as a whole: a finding is in
the output iff it is produced by (at least) one of the eight rules. -/
theorem mem_runAll {cfg : Config} {today : Int} {invs : List Inv}
    {histRows rawRows : List Row} {f : Finding} :
    f ∈ runAll cfg today invs histRows rawRows ↔
      f ∈ dupFindings invs ∨ f ∈ lagFindings cfg invs ∨
      f ∈ overdueFindings cfg today invs ∨ f ∈ outlierFindings cfg invs ∨
      f ∈ rateFindings cfg histRows ∨ f ∈ newChargeFindings histRows ∨
      f ∈ creditFindings rawRows ∨ f ∈ taxFindings histRows := by
  simp [runAll, List.mem_append]

def totalFlagged (fs : List Finding) : Nat :=
  (fs.map (fun f => f.amount.natAbs)).sum

/-- Conservation, stated exactly as the code computes it: the reported
total is the sum of |amount| over the eight rule outputs, in order.
Nothing is lost and nothing is added by the concatenation itself — all
over/under-counting relative to *invoice* exposure comes from what the
individual findings' amounts are (next theorems). -/
theorem totalFlagged_runAll (cfg : Config) (today : Int) (invs : List Inv)
    (histRows rawRows : List Row) :
    totalFlagged (runAll cfg today invs histRows rawRows) =
      totalFlagged (dupFindings invs) + totalFlagged (lagFindings cfg invs) +
      totalFlagged (overdueFindings cfg today invs) +
      totalFlagged (outlierFindings cfg invs) +
      totalFlagged (rateFindings cfg histRows) +
      totalFlagged (newChargeFindings histRows) +
      totalFlagged (creditFindings rawRows) +
      totalFlagged (taxFindings histRows) := by
  simp [runAll, totalFlagged, List.map_append, List.sum_append, Nat.add_assoc]

/-- Aggregation is per-*finding*: the same invoice flagged by two rules
contributes its amount twice to the KPI. Concrete: one unpaid invoice,
1000 total, entered 100 days after issue and 100 days overdue, is
counted 2000 by the dashboard although only 1000 is at issue. -/
theorem kpi_double_counts_multi_flagged_invoice :
    totalFlagged (runAll defaultConfig 100 [
      { id := 1, supplier := some 1, number := "INV-9".toList, total := 1000, paid := 0,
        status := 2, issued := 0, created := 100, required := none }] [] [])
      = 2000 := by decide

/-- Aggregation undercounts duplicate exposure: for a duplicate group the
finding amount (hence the KPI contribution) is the group total *minus
the first copy in row order* (`dup_amount_identity`). Concrete: two
copies of a 500 invoice contribute 500 to the KPI, not 1000. -/
theorem kpi_duplicate_undercounts :
    totalFlagged (runAll defaultConfig 10 [
      { id := 1, supplier := some 1, number := "INV-1".toList, total := 500, paid := 0,
        status := 1, issued := 0, created := 1, required := none },
      { id := 2, supplier := some 1, number := "inv1".toList, total := 500, paid := 0,
        status := 1, issued := 0, created := 1, required := none }] [] [])
      = 500 := by decide

/-- Negative finding amounts contribute *positively* to the KPI (the
dashboard takes `abs`): an overdue credit invoice (total −5000) raises
`total_flagged` by 5000. -/
theorem kpi_abs_of_negative_amounts :
    totalFlagged (runAll defaultConfig 100 [
      { id := 1, supplier := some 1, number := "INV-2".toList, total := -5000, paid := 0,
        status := 2, issued := 0, created := 1, required := none }] [] [])
      = 5000 := by decide

end InvoiceAudit
