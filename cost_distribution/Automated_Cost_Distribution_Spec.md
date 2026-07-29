# PROMPT — Python ETL for the *Automated Cost Distribution* Model

> **How to use this prompt.** Paste everything below the line into a code-capable LLM (or hand it to a developer) together with the source file `Automated Cost Distribution V.05.xlsx`. It is a complete functional specification: it defines the source data contract, the cost-distribution business logic, the ordered ETL pipeline, the output contract, and the reconciliation controls needed to reproduce the workbook's `Distribution` sheet programmatically in Python.

---

## ROLE

You are a **data engineer specialising in management-accounting / cost-allocation pipelines**. You write production-grade, auditable Python (pandas) ETL. You treat every allocation as a controlled transformation that must reconcile to source to the cent.

## OBJECTIVE

Reproduce, in Python, the automated cost-distribution engine currently implemented in the Excel workbook `Automated Cost Distribution V.05.xlsx`. The engine takes a **General Ledger** of cost transactions and explodes each line into one or more **allocated rows** across receiving departments, driven by a rule table (`LOGIC`) and a basis/percentage table (`ALLOCATION`). The deliverable is a single tidy output table identical in grain and totals to the workbook's `Distribution` sheet.

The pipeline must be **parameterised, idempotent, and self-reconciling**: re-running it on the same inputs yields the same output, and total allocated cost must equal total source cost.

---

## 1. SOURCE DATA CONTRACT

Input: one Excel workbook with the sheets below. Load each into its own DataFrame. Trim whitespace on all string keys before joining. Do **not** assume column order — select by header name.

### 1.1 `GL` — General Ledger (fact / grain = one journal line)
| Column | Type | Notes |
|---|---|---|
| Date | date | Transaction date |
| Type | str | Journal type (CD, PJ, …) |
| Ref No. | str | Document reference (keep as text; may contain `/`, leading zeros) |
| Contact | str | |
| Description | str | |
| Note | str | Nullable |
| Dept | str | **Originating cost centre** — primary allocation key |
| Project | str | e.g. `N/A` |
| Curr | str | Original currency (IDR/EUR/…); amounts already in IDR |
| Debit | float | |
| Credit | float | |
| Balance | float | Ignore for allocation (source figure is Debit − Credit) |
| Account Code | str | e.g. `5220-40-000` — join key to `LOGIC`, `COA` |
| Account Name | str | |
| Note2 | str | Nullable reviewer annotation (V.05); carried to `gl_entry`, not used in allocation |

**Derived measure:** `Amount = Debit − Credit` for cost, and `Amount = Credit − Debit` for accounts whose natural balance is a credit (revenue, `4xxx` — see `Config.credit_balance_prefixes`). Both sides therefore read **positive**: the distribution shows what each department earned *and* what it spent, each in its own sign, rather than revenue appearing as negative cost.

Sign is preserved within each side: a reversal stays negative. A credit note reduces revenue, so its `Amount` is negative — that is correct, not something to force positive.

> **The grand total is now cost + revenue.** It still ties out (allocated == source) but is a mixed figure; read the two apart by account group (`Code LIKE '4%'`).

### 1.2 `COA` — Chart of Accounts (dimension, key = Code)
`Code` → `Account Name` → `Reporting Code` → `Reporting Line` → `Reporting Account`. Supplies three output columns: `Reporting Code`, `Reporting Account Name` (= `Reporting Line`) and `Reporting Account` (= `"<Reporting Code> <Reporting Line>"`).

As of V.05 the sheet covers the **whole** chart (balance sheet included, ~294 accounts), not just cost accounts. Workbooks without the `Reporting Code`/`Reporting Account` columns (V.04 and earlier) still load: the code resolves to null and the label degrades to the reporting line alone.

> **Authority:** `COA` is the source of truth for reporting codes. The V.05 `Distribution` sheet still shows a *legacy* numbering (`503`, `604`, `660`) that predates the current one (`012`, `200`, `360`); the pipeline follows `COA`/`RL`, so those two columns will not match that sheet.

### 1.2b `RL` — Reporting-line master (dimension, new in V.05)
`Head Code`, `Head Description`, `Reporting Line`, `reporting_line_name`, `reporting_code`, `Description` — the group reporting hierarchy the `COA` reporting codes belong to. **Reference only:** the pipeline reads each account's resolved line from `COA`, so `RL` is loaded and stored (`basis_rl`) but drives no allocation. Absent in older workbooks; its absence is tolerated.

### 1.3 `PC` — Cost-/Profit-Centre master (dimension, key = Dept)
`Dept Code`, `Dept`, `Div`, `PC`. Two independent uses:
- **Rule bucketing:** `PC.PC` for a given `Dept` is the *bucket* used to look up the distribution rule (values such as `Head Quarter`, `Laboratory Operation Support`, `System Certification Services`, `Product Certification & Testing`, `Surabaya Representative Office`, `Medan Representative Office`, `BUSINESS SUPPORT`).
- **Output enrichment:** supplies `Dept_div` and `PC` for the **receiving** department (`New Dept`).
- **Overhead scope:** the bucket `Administration Indonesia` marks the departments swept in Step E.

> **V.05 renames** (they run through `PC`, `LOGIC` and `ALLOCATION` together): bucket `CERTIFICATION SERVICES` → `System Certification Services`; dept `System Certification Services` → `Integrated Management System`; dept `Automotive (System Certification Services Technical Ops)` → `Automotive`; method `Revenue - Certification Services` → `Revenue - System Certification Services`.

### 1.4 `LOGIC` — Distribution rule table (grain = Account Code × PC bucket)
| Column | Role |
|---|---|
| Account Code | join key (with bucket) |
| Account Name | descriptive |
| PC | **bucket** (matches `PC.PC`) — second half of the composite rule key |
| Distribution | **method name** — join key into `ALLOCATION` |
| Code | descriptive composite id |

A single `Account Code` can carry different methods depending on the bucket (e.g. `5221-50-000` → `Revenue HO` under *Head Quarter*, `Lab Distribution` under *Laboratory Operation Support*, `Revenue - System Certification Services` under *System Certification Services*, `PE Product Distribution` under *Product Certification & Testing*).

Observed method vocabulary (do not hard-code; read from data): `Lab Distribution`, `Revenue HO`, `Revenue`, `Revenue - System Certification Services`, `PE Product Distribution`, `Corporate Services`, `Surabaya Distribution`, `FTE - All`, `FTE - Head Office`, `FTE - Surabaya`, `FTE - Medan`, `FTE - Laboratory`, `Head Office : Tower G`, `Head Office : Tower B`.

### 1.5 `ALLOCATION` — Basis / split-factor table (the driver)
| Column | Role |
|---|---|
| Distribution | method name (join key from `LOGIC.Distribution`) |
| Account Name | **only populated for `Lab Distribution`** (split varies by account); NULL for all other methods |
| New Dept | receiving department |
| Percentage | split factor for that (method [, account] , New Dept) |

Semantics:
- For **FTE-\*** and **Revenue-\*** and Tower/Corporate/Surabaya methods: percentages depend on the **method only** — join on `Distribution`.
- For **`Lab Distribution`**: percentages depend on `Distribution` **and** `Account Name` — join on both.
- Within each method key, the percentages are designed to sum to 1 (verify — see §5).

### 1.6 `FTE` — Headcount register (basis provenance, key = employee)
`FTE`, `HC`, `Name`, `Employee_No.`, `Dept`, `Div`, `PC`, `Location`, `Location Detail`. Source of truth **behind** the headcount-driven percentages in `ALLOCATION` (headcount share by department within a scope). Treat `ALLOCATION` as authoritative for the base run; recompute is **optional** so bases stay refreshable and auditable.

Scopes (`Config.fte_scopes`) — each is *(FTE column, substring to match)*:

| Method | Scope |
|---|---|
| `FTE - All` | every employee |
| `FTE - Head Office` | `Location` contains *Head Office* |
| `FTE - Laboratory` | `Location` contains *Laboratory* |
| `FTE - Surabaya` | `Location` contains *Surabaya* |
| `FTE - Medan` | `Location` contains *Medan* |
| `Head Office : Tower G` | `Location Detail` contains *Head Office : Tower G* |
| `Head Office : Tower B` | `Location Detail` contains *Head Office : Tower B* |

The two Tower methods scope on **`Location Detail`**: `Location` only goes down to *Head Office* and cannot tell the towers apart.

### 1.5b Basis map — where each method's percentages come from

| Basis | Methods |
|---|---|
| `FTE` / `basis_fte` | `FTE - All`, `FTE - Head Office`, `FTE - Laboratory`, `FTE - Surabaya`, `FTE - Medan`, `Head Office : Tower G`, `Head Office : Tower B` |
| `REV` / `basis_rev` | `Revenue HO`, `Revenue`, `Revenue - System Certification Services` |
| **Fixed** (hand-maintained, passes through untouched) | `Lab Distribution`, `Corporate Services`, `Surabaya Distribution`, `PE Product Distribution`, `Bank Administrative` |

**`Bank Administrative`** is the "one account, one destination" shape: `6924-00-000` is routed to `Revenue & Investment Controlling` in full, wherever it originates. Because the rule key includes the PC bucket, it needs **one `LOGIC` row per bucket** (14 of them) — a single row would only catch cost arising in that one bucket. The originating `Dept` is still recorded on every row, so the source of the charge remains traceable.

> These two rules live in `basis_logic` / `basis_allocation`, **not** in the workbook. `--import-basis --reseed-global` refreshes the policy tables from the workbook and would drop them; add them to the workbook's `LOGIC`/`ALLOCATION` sheets if you re-seed from Excel.

Anything not in the first two rows is Fixed by definition. The recompute logs the three groups on every run so the result can be checked against this table without reading config.

The label is persisted per row in `basis_allocation.basis`, so the map is queryable in SQL. It is always derived from the method name (`cost_distribution.basis.basis_of`) and written by every path that fills the table, so it cannot drift from the method it describes.

### 1.7 `REV` — Revenue basis (basis provenance)
`Div`, `PC`, `Location`, `Amount`, `Percentage Certification Services`, `Percentage HO`, `Percentage All`. Source of truth behind the `Revenue*` percentages in `ALLOCATION`. Map: `Revenue HO` → *Percentage HO*, `Revenue - System Certification Services` → *Percentage Certification Services* (the REV column keeps its V.04 name; only the method was renamed), `Revenue`/`Revenue All` → *Percentage All*. Same treatment as FTE: `ALLOCATION` authoritative; recompute optionally.

### 1.8 `INFO`, `Updt` — metadata only
Field-mapping notes and a version/change log. Not used in computation; you may parse `INFO` to auto-derive the output field map.

---

## 2. BUSINESS LOGIC — the distribution engine

For every GL line, decide **direct charge** vs **distributed**, then produce allocated rows.

**Step A — Enrich.** Left-join `GL` to `PC` on `Dept` to obtain the **bucket** = `PC.PC(GL.Dept)`. Left-join to `COA` on `Account Code` for `Reporting Code`, `Reporting Line` and `Reporting Account`.

**Step B — Resolve rule.** Left-join `GL` to `LOGIC` on the composite key **(`Account Code`, bucket)** → `method = LOGIC.Distribution`.

**Step C — Branch per line:**
- **No rule matched (`method` is NULL) → DIRECT CHARGE.** Emit exactly **one** row: `New Dept = Dept`, `Percentage = 1`, `Allocation = Amount`.
- **Rule matched → DISTRIBUTE.** Join to `ALLOCATION` to fetch every `(New Dept, Percentage)` for that method:
  - if `method == "Lab Distribution"`: match on `Distribution == method` **AND** `ALLOCATION.Account Name == GL.Account Name`;
  - else: match on `Distribution == method` only.
  - Emit **one row per receiving `New Dept`**. `Allocation = Amount × Percentage`.

**Step D — Receiving-side enrichment.** For every emitted row, set `Dept_div` and `PC` from `PC` master looked up on **`New Dept`** (not the original `Dept`).

**Invariant:** for each source GL line, `Σ Allocation (child rows) == Amount (source line)`. This holds because direct charges use Percentage = 1 and distributed methods use percentages that sum to 1.

**Step E — Overhead sweep (new in V.05).** After Steps A–D, the administrative departments do not keep the cost they have accumulated: it is pushed onto the revenue-generating departments. Per period:

1. **Reverse.** For every dept whose `PC` bucket is `Administration Indonesia` and whose Step-D total is non-zero, emit one row at `Percentage = 1` with `Allocation = −(its net position)`. `New Dept = Dept =` that dept.

   **Net, not gross.** Revenue earned by an administrative department settles against that department's own cost first; only what remains is pushed out. Both sides are stored positive, so revenue enters the pool with its sign flipped: `net = Σ cost − Σ revenue`. A department that earns more than it spends produces a negative pool, which the sweep hands out as a negative charge — still zero-sum, no special case.
2. **Pool.** `pool = Σ` of the reversed amounts.
3. **Charge out.** Take the `FTE - All` roster from `ALLOCATION`, drop the swept (administrative) depts, renormalise the remaining shares over their own sum and **round to 4 decimals** — the precision the workbook states. Emit one row per receiving dept with `Amount = pool`, `Percentage = share`, `Allocation = pool × share`.

These rows are **derived, not sourced**: they carry no `gl_line_id`, `Type`, `Ref No.` or `Contact`. They are booked to COA account `7501-00-000 Allocation Overhead`, dated the 1st of the period, described `Allocation Overhead <MMYY>`, and labelled `FTE : PC <previous month, in Indonesian>` (e.g. `FTE : PC Juni` for a July run).

> **Zero-sum invariant:** the block sums to 0, so the grand total is unchanged and Step E can never create or destroy cost. Rounding to 4 dp can leave the shares fractionally off 1.0; the residual is added to the largest receiver so the invariant holds exactly (and a warning is logged if it was more than float noise).

> Disable with `--no-overhead` to see where cost lands before the sweep.

> **Sign handling:** never take absolute values. A credit (negative `Amount`) distributes to proportionally negative allocations.

> **Unmatched-method guard:** if a line resolves to a `method` that has **no** rows in `ALLOCATION` (or, for `Lab Distribution`, no rows for that `Account Name`), do **not** silently drop it. Route it to a `rejects` frame and log it (see §5).

---

## 3. ETL PIPELINE (ordered)

**Extract**
1. Read every required sheet with `pandas.read_excel(sheet_name=…, dtype={... ref/codes as str})`. Keep `Ref No.`, `Account Code`, `Dept Code` as strings.
2. Normalise: strip whitespace, standardise NULLs, coerce `Debit`/`Credit`/`Percentage`/`Amount` to numeric (`errors="raise"`), parse `Date`.
3. Build lookup frames: `dept_to_bucket`, `dept_to_div_pc`, `code_to_reporting{,_code,_acct}`, `rule` (`LOGIC`), `alloc` (`ALLOCATION`), `sweep_depts` (PC bucket = `Administration Indonesia`).

**Transform**
4. Compute `Amount = Debit − Credit` on `GL`.
5. Attach bucket + reporting line (Step A).
6. Resolve `method` (Step B).
7. Split into `direct` and `to_distribute` (Step C predicate = `method.isna()`).
8. `direct`: assign `New Dept = Dept`, `Percentage = 1.0`, `Allocation = Amount`.
9. `to_distribute`: merge to `alloc` (conditional key for `Lab Distribution`), compute `Allocation = Amount * Percentage`. Capture unmatched → `rejects`.
10. `pd.concat([direct, distributed])`; enrich `Dept_div`/`PC` from `New Dept` (Step D); build derived string columns (§4).
11. Sweep administrative overhead onto the revenue-generating depts (Step E), producing a separate zero-sum block.
12. Run validation gate (§5) against the Step A–D output plus the Step-E block. Fail the run (raise) on any breach, then concatenate the two into the final frame.

**Load**
13. Write the tidy `Distribution` frame plus a `Reconciliation` summary and any `rejects` to an output workbook (`openpyxl`/`xlsxwriter`) and/or Parquet. Preserve column order per §4.

---

## 4. OUTPUT CONTRACT — `Distribution` (target, 22 columns, in order)

V.05 added `Reporting Code` and `Reporting Account`, renamed `Div` → `Dept_div`, and lower-cased the `Date` header to `date`.

| # | Column | Source / formula |
|---|---|---|
| 1 | date | GL.Date (1st of the period for Step-E rows) |
| 2 | Type | GL.Type |
| 3 | Ref No. | GL.`Ref No.` |
| 4 | Contact | GL.Contact |
| 5 | Description | GL.Description |
| 6 | Note | GL.Note |
| 7 | Code | GL.`Account Code` |
| 8 | Account Name | GL.`Account Name` |
| 9 | Account | `Code & " " & Account Name` |
| 10 | Reporting Code | COA.`Reporting Code` via Code |
| 11 | Reporting Account Name | COA.`Reporting Line` via Code |
| 12 | Reporting Account | COA.`Reporting Account` via Code |
| 13 | Dept | **raw** GL.Dept (unchanged) |
| 14 | New Dept | receiving dept (= Dept for direct charges) |
| 15 | Dept_div | PC.Div via **New Dept** |
| 16 | PC | PC.PC via **New Dept** |
| 17 | Debit | GL.Debit |
| 18 | Credit | GL.Credit |
| 19 | Amount | Debit − Credit |
| 20 | Percentage | 1 (direct) or ALLOCATION.Percentage |
| 21 | Allocation | Amount × Percentage |
| 22 | Distribution And Allocation | audit label — `method / New Dept` for distributed rows, the sweep label for Step E, blank for direct charges |

The pipeline carries the stable internal names (`Date`, `Div`) end to end and applies the workbook's spelling only when writing the sheet (`config.OUTPUT_HEADERS`), so the DB mapping and every transform stay on one vocabulary.

---

## 5. VALIDATION & RECONCILIATION (hard gates — fail closed)

1. **Grand-total tie-out:** `Distribution.Allocation.sum()` == `GL.Amount.sum()` within `max(1e-6, |source| × 1e-13)` (report absolute variance). The relative arm absorbs float64 summation noise, which grows with the size of the total — a ~2.5e10 total accumulates ~1e-5 of noise, far below a cent but above a flat `1e-6`.
1b. **Overhead sweep is zero-sum:** the Step-E block nets to 0 within the same tolerance, so it can only move cost between departments.
2. **Per-line conservation:** for each GL line, `Σ child Allocation == Amount` (tolerance `1e-6`). Applied to the Step A–D output; Step-E rows have no source line and are excluded.
3. **Percentage integrity:** for each distributed `(method [, Account Name])` key present in the run, factors sum to `1.0 ± 1e-6`; warn on keys that don't.
4. **No orphan rules:** every resolved `method` exists in `ALLOCATION`; `rejects` frame must be empty (or explicitly signed-off).
5. **Referential integrity:** every `Dept` and `New Dept` exists in `PC`; every `Account Code` exists in `COA`. List misses.
6. **Row-count sanity:** direct rows == count of GL lines without a rule; distributed rows == Σ receiving-dept counts per matched method.
7. **Sign check:** count and total of negative allocations reconcile to negative source lines.

Emit a `Reconciliation` sheet: source total, allocated total, variance, #GL lines, #output rows, #direct, #distributed, #overhead, overhead pool, #rejects.

---

## 6. NON-FUNCTIONAL REQUIREMENTS

- **Stack:** Python ≥3.10, `pandas`, `numpy`, `openpyxl`/`xlsxwriter`; optional `pandera` for schema validation, `structlog`/`logging` for the audit trail.
- **Config-driven:** input path, sheet names, tolerance, output path, and the `Lab Distribution` special-case flag in a config block/`.yaml` — no magic strings in logic.
- **Idempotent & pure:** transformations are functions of inputs only; no in-place mutation of source frames; deterministic ordering (sort by GL index then New Dept).
- **Vectorised:** use merges/`groupby`, not row loops. The `Lab Distribution` conditional join is the only branch.
- **Auditability:** carry a stable `gl_line_id` from source through to every child row so any allocated figure traces to its journal line.
- **Structure:** modular functions — `extract()`, `build_lookups()`, `resolve_rule()`, `distribute()`, `enrich()`, `validate()`, `load()` — plus a `main()` orchestrator and a `--dry-run` that runs validation without writing.

---

## 7. EDGE CASES TO HANDLE EXPLICITLY

- GL line with **zero** `Amount` (Debit == Credit) → still emit rows; allocations are 0.
- **Whitespace / casing** mismatches between `GL.Dept` and `PC.Dept`, or `GL.Account Name` and `ALLOCATION.Account Name` (critical for `Lab Distribution`) → normalise before joining; report residual misses.
- `Lab Distribution` account present in `LOGIC` but **missing** in `ALLOCATION` for that account name → reject + log, do not drop.
- **Percentage set ≠ 1** for a method → warn, still process, surface variance in reconciliation.
- **Duplicate** `LOGIC` keys `(Account Code, bucket)` → detect and fail (ambiguous rule).
- Non-IDR `Curr` rows → amounts are assumed pre-converted to IDR; assert no FX step is required, or flag if a rate table is later introduced.

---

## 8. DELIVERABLES

1. A runnable Python module/package implementing §3 with the function structure in §6.
2. The output workbook: `Distribution` (§4) + `Reconciliation` (§5) + `rejects` (if any).
3. A short **methodology note** (stakeholder-facing): inputs, the rule→basis mechanism, controls, and reconciliation result — written so a controller can sign off the allocation without reading code.

**Begin by loading the workbook, printing the inferred schema of each sheet, and restating the rule→basis→output mapping you will implement before writing the pipeline.**
