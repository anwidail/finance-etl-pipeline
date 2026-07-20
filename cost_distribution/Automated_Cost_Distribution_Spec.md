# PROMPT — Python ETL for the *Automated Cost Distribution* Model

> **How to use this prompt.** Paste everything below the line into a code-capable LLM (or hand it to a developer) together with the source file `Automated_Cost_Distribution_V_04.xlsx`. It is a complete functional specification: it defines the source data contract, the cost-distribution business logic, the ordered ETL pipeline, the output contract, and the reconciliation controls needed to reproduce the workbook's `Distribution` sheet programmatically in Python.

---

## ROLE

You are a **data engineer specialising in management-accounting / cost-allocation pipelines**. You write production-grade, auditable Python (pandas) ETL. You treat every allocation as a controlled transformation that must reconcile to source to the cent.

## OBJECTIVE

Reproduce, in Python, the automated cost-distribution engine currently implemented in the Excel workbook `Automated_Cost_Distribution_V_04.xlsx`. The engine takes a **General Ledger** of cost transactions and explodes each line into one or more **allocated rows** across receiving departments, driven by a rule table (`LOGIC`) and a basis/percentage table (`ALLOCATION`). The deliverable is a single tidy output table identical in grain and totals to the workbook's `Distribution` sheet.

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

**Derived measure:** `Amount = Debit − Credit` (credits produce negative cost; preserve sign throughout).

### 1.2 `COA` — Chart of Accounts (dimension, key = Code)
`Code` → `Account Name` → `Reporting Line`. Supplies the `Reporting Account Name` in the output.

### 1.3 `PC` — Cost-/Profit-Centre master (dimension, key = Dept)
`Dept Code`, `Dept`, `Div`, `PC`. Two independent uses:
- **Rule bucketing:** `PC.PC` for a given `Dept` is the *bucket* used to look up the distribution rule (values such as `Head Quarter`, `Laboratory Operation Support`, `CERTIFICATION SERVICES`, `Product Certification & Testing`, `Surabaya Representative Office`, `Medan Representative Office`, `BUSINESS SUPPORT`).
- **Output enrichment:** supplies `Div` and `PC` for the **receiving** department (`New Dept`).

### 1.4 `LOGIC` — Distribution rule table (grain = Account Code × PC bucket)
| Column | Role |
|---|---|
| Account Code | join key (with bucket) |
| Account Name | descriptive |
| PC | **bucket** (matches `PC.PC`) — second half of the composite rule key |
| Distribution | **method name** — join key into `ALLOCATION` |
| Code | descriptive composite id |

A single `Account Code` can carry different methods depending on the bucket (e.g. `5221-50-000` → `Revenue HO` under *Head Quarter*, `Lab Distribution` under *Laboratory Operation Support*, `Revenue - Certification Services` under *CERTIFICATION SERVICES*, `PE Product Distribution` under *Product Certification & Testing*).

Observed method vocabulary (do not hard-code; read from data): `Lab Distribution`, `Revenue HO`, `Revenue`, `Revenue - Certification Services`, `PE Product Distribution`, `Corporate Services`, `Surabaya Distribution`, `FTE - All`, `FTE - Head Office`, `FTE - Surabaya`, `FTE - Medan`, `FTE - Laboratory`, `Head Office : Tower G`, `Head Office : Tower B`.

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
`FTE`, `HC`, `Name`, `Employee_No.`, `Dept`, `Div`, `PC`, `Location`, `Location Detail`. Source of truth **behind** the `FTE - *` percentages in `ALLOCATION` (headcount share by department within a scope). Treat `ALLOCATION` as authoritative for the base run; expose an **optional** function to recompute `FTE - *` factors from this sheet so bases stay refreshable and auditable.

### 1.7 `REV` — Revenue basis (basis provenance)
`Div`, `PC`, `Location`, `Amount`, `Percentage Certification Services`, `Percentage HO`, `Percentage All`. Source of truth behind the `Revenue*` percentages in `ALLOCATION`. Map: `Revenue HO` → *Percentage HO*, `Revenue - Certification Services` → *Percentage Certification Services*, `Revenue`/`Revenue All` → *Percentage All*. Same treatment as FTE: `ALLOCATION` authoritative; recompute optionally.

### 1.8 `INFO`, `Updt` — metadata only
Field-mapping notes and a version/change log. Not used in computation; you may parse `INFO` to auto-derive the output field map.

---

## 2. BUSINESS LOGIC — the distribution engine

For every GL line, decide **direct charge** vs **distributed**, then produce allocated rows.

**Step A — Enrich.** Left-join `GL` to `PC` on `Dept` to obtain the **bucket** = `PC.PC(GL.Dept)`. Left-join to `COA` on `Account Code` for `Reporting Line`.

**Step B — Resolve rule.** Left-join `GL` to `LOGIC` on the composite key **(`Account Code`, bucket)** → `method = LOGIC.Distribution`.

**Step C — Branch per line:**
- **No rule matched (`method` is NULL) → DIRECT CHARGE.** Emit exactly **one** row: `New Dept = Dept`, `Percentage = 1`, `Allocation = Amount`.
- **Rule matched → DISTRIBUTE.** Join to `ALLOCATION` to fetch every `(New Dept, Percentage)` for that method:
  - if `method == "Lab Distribution"`: match on `Distribution == method` **AND** `ALLOCATION.Account Name == GL.Account Name`;
  - else: match on `Distribution == method` only.
  - Emit **one row per receiving `New Dept`**. `Allocation = Amount × Percentage`.

**Step D — Receiving-side enrichment.** For every emitted row, set `Div` and `PC` from `PC` master looked up on **`New Dept`** (not the original `Dept`).

**Invariant:** for each source GL line, `Σ Allocation (child rows) == Amount (source line)`. This holds because direct charges use Percentage = 1 and distributed methods use percentages that sum to 1.

> **Sign handling:** never take absolute values. A credit (negative `Amount`) distributes to proportionally negative allocations.

> **Unmatched-method guard:** if a line resolves to a `method` that has **no** rows in `ALLOCATION` (or, for `Lab Distribution`, no rows for that `Account Name`), do **not** silently drop it. Route it to a `rejects` frame and log it (see §5).

---

## 3. ETL PIPELINE (ordered)

**Extract**
1. Read every required sheet with `pandas.read_excel(sheet_name=…, dtype={... ref/codes as str})`. Keep `Ref No.`, `Account Code`, `Dept Code` as strings.
2. Normalise: strip whitespace, standardise NULLs, coerce `Debit`/`Credit`/`Percentage`/`Amount` to numeric (`errors="raise"`), parse `Date`.
3. Build lookup frames: `dept_to_bucket`, `dept_to_div_pc`, `code_to_reporting`, `rule` (`LOGIC`), `alloc` (`ALLOCATION`).

**Transform**
4. Compute `Amount = Debit − Credit` on `GL`.
5. Attach bucket + reporting line (Step A).
6. Resolve `method` (Step B).
7. Split into `direct` and `to_distribute` (Step C predicate = `method.isna()`).
8. `direct`: assign `New Dept = Dept`, `Percentage = 1.0`, `Allocation = Amount`.
9. `to_distribute`: merge to `alloc` (conditional key for `Lab Distribution`), compute `Allocation = Amount * Percentage`. Capture unmatched → `rejects`.
10. `pd.concat([direct, distributed])`; enrich `Div`/`PC` from `New Dept` (Step D); build derived string columns (§4).
11. Run validation gate (§5). Fail the run (raise) on any breach.

**Load**
12. Write the tidy `Distribution` frame plus a `Reconciliation` summary and any `rejects` to an output workbook (`openpyxl`/`xlsxwriter`) and/or Parquet. Preserve column order per §4.

---

## 4. OUTPUT CONTRACT — `Distribution` (target, 20 columns, in order)

| # | Column | Source / formula |
|---|---|---|
| 1 | Date | GL.Date |
| 2 | Type | GL.Type |
| 3 | Ref No. | GL.`Ref No.` |
| 4 | Contact | GL.Contact |
| 5 | Description | GL.Description |
| 6 | Note | GL.Note |
| 7 | Code | GL.`Account Code` |
| 8 | Account Name | GL.`Account Name` |
| 9 | Account | `Code & " " & Account Name` |
| 10 | Reporting Account Name | COA.`Reporting Line` via Code |
| 11 | Dept | **raw** GL.Dept (unchanged) |
| 12 | New Dept | receiving dept (= Dept for direct charges) |
| 13 | Div | PC.Div via **New Dept** |
| 14 | PC | PC.PC via **New Dept** |
| 15 | Debit | GL.Debit |
| 16 | Credit | GL.Credit |
| 17 | Amount | Debit − Credit |
| 18 | Percentage | 1 (direct) or ALLOCATION.Percentage |
| 19 | Allocation | Amount × Percentage |
| 20 | Distribution And Allocation | optional audit label, e.g. `method` name / `New Dept`; leave blank if not required |

---

## 5. VALIDATION & RECONCILIATION (hard gates — fail closed)

1. **Grand-total tie-out:** `Distribution.Allocation.sum()` == `GL.Amount.sum()` within tolerance `1e-6` (report absolute variance).
2. **Per-line conservation:** for each GL line, `Σ child Allocation == Amount` (tolerance `1e-6`).
3. **Percentage integrity:** for each distributed `(method [, Account Name])` key present in the run, factors sum to `1.0 ± 1e-6`; warn on keys that don't.
4. **No orphan rules:** every resolved `method` exists in `ALLOCATION`; `rejects` frame must be empty (or explicitly signed-off).
5. **Referential integrity:** every `Dept` and `New Dept` exists in `PC`; every `Account Code` exists in `COA`. List misses.
6. **Row-count sanity:** direct rows == count of GL lines without a rule; distributed rows == Σ receiving-dept counts per matched method.
7. **Sign check:** count and total of negative allocations reconcile to negative source lines.

Emit a `Reconciliation` sheet: source total, allocated total, variance, #GL lines, #output rows, #direct, #distributed, #rejects.

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
