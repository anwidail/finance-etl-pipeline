# Automated Cost Distribution — Methodology Note

*Stakeholder-facing summary. A controller can sign off the allocation from this
page without reading code.*

## What it does

Every cost line in the **General Ledger** (`GL`, 2.437 lines) is pushed to the
department(s) that should ultimately bear the cost. A line is either:

- **charged directly** to its originating department (no distribution rule), or
- **distributed** across several *receiving* departments using a fixed set of
  split percentages.

The result is one tidy `Distribution` table (3.700 rows) that reproduces the
Excel workbook exactly.

## The rule → basis mechanism

1. **Which bucket?** Each originating department maps to a *profit-centre bucket*
   via the `PC` master (e.g. *Head Quarter*, *Laboratory Operation Support*).
2. **Which method?** The pair **(Account Code, Account Name, bucket)** looks up a
   *distribution method* in the `LOGIC` rule table (e.g. `FTE - Head Office`,
   `Revenue HO`, `Lab Distribution`). No rule ⇒ **direct charge**.
3. **What split?** The method looks up its receiving departments and percentages
   in the `ALLOCATION` basis table. `Lab Distribution` splits differ by account,
   so it is keyed on the account name as well.
4. **Enrich.** Each output row gets the receiving department's `Div`/`PC` and the
   account's `Reporting Line`.

The split percentages themselves trace back to headcount (`FTE`) and revenue
(`REV`) bases; `ALLOCATION` is treated as authoritative for this run.

## Controls (the run fails closed on any breach)

| # | Control | Result |
|---|---|---|
| 1 | Grand-total tie-out (allocated = source) | **✓ variance 0.00** |
| 2 | Per-line conservation (Σ children = line amount) | **✓** |
| 3 | Percentage sets sum to 1 per method | ✓ (warns otherwise) |
| 4 | No orphan rules (every method has a basis) | **✓ 0 rejects** |
| 5 | Referential integrity (Dept/Code exist in masters) | ✓ misses listed |
| 6 | Row-count sanity (direct vs distributed) | **✓ 2.225 direct / 1.475 distributed** |
| 7 | Sign check (credits stay negative) | **✓ −776.550.095,96 both sides** |

Sign is preserved throughout (`Amount = Debit − Credit`); a credit distributes to
proportionally negative allocations — no absolute values are ever taken.

## Reconciliation result

| Metric | Value |
|---|---|
| Source total (GL) | **Rp 15.282.090.268,93** |
| Allocated total (Distribution) | **Rp 15.282.090.268,93** |
| Variance | **Rp 0,00** |
| GL lines / output rows | 2.437 / 3.700 |
| Direct / distributed / rejects | 2.225 / 1.475 / 0 |

Validated against the workbook's own `Distribution` sheet: all 1.059
`(Account, Receiving Dept)` groups match, total residual **Rp 0,01** (one-cent
rounding across Rp 15,3 bn).

## How to run

```bash
# validate only (no file written)
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --dry-run

# full run → cost_distribution/output/Distribution_output.xlsx
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline

# recompute FTE-*/Revenue-* factors from the FTE/REV sheets, and load to MySQL
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --recompute-basis --to-db
```

Output workbook contains `Distribution`, `Reconciliation`, and (if any) `rejects`.
Every output row carries a `gl_line_id` internally so any allocated figure traces
back to its source journal line. See `Automated_Cost_Distribution_Spec.md` for
the full functional specification.

## Editable basis in MySQL

The reference basis can be maintained in `cost_distribution_db` instead of Excel,
so it can be edited via SQL or a future app. There are two kinds:

| Kind | Tables | Period? | Changes |
|---|---|---|---|
| **Policy** | `basis_pc`, `basis_coa`, `basis_logic` | no | only on a policy change |
| **Monthly** | `basis_allocation`, `basis_fte`, `basis_rev` | `period` (MMM-YYYY) | each month |

Policy tables hold a single current version; the monthly tables carry a `period`
so every month has its own independently-editable split factors. The period
label is `MMM-YYYY` (e.g. `APR-2026`), derived from the GL date's month; the CLI
also accepts `YYYY-MM` and normalises it.

```bash
# 1. seed: policy tables once (skipped if already present), the month's factors
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --import-basis --period APR-2026
#    to also refresh the policy tables from the workbook, add --reseed-global

# 2. edit the basis_* tables in MySQL as needed

# 3. run using the DB basis for that period (GL still from the workbook feed)
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline \
    --basis-from-db --period APR-2026 --to-db
```

### Fully DB-driven month

The GL fact can also live in MySQL (`gl_entry`, period-scoped), so a whole month
runs without the workbook — the shape an app would drive:

```bash
# seed the month's GL from the workbook (only that month's lines)
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --import-gl --period APR-2026

# run entirely from the database: GL + basis from MySQL, snapshot back to MySQL
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline \
    --gl-from-db --basis-from-db --period APR-2026 --to-db
```

`gl_entry` stores Debit/Credit at `DECIMAL(28,12)` so FX-converted lines keep
their sub-cent precision and the source total matches the workbook to the cent.

### Updating FTE / REV and recompute

Edit `basis_fte` (headcount) or `basis_rev` (revenue) for the period, then run
with `--recompute-basis`: the FTE-*/Revenue-* factors are recomputed and
**written straight back into `basis_allocation`** for that period (a full
replace — no duplicate rows accumulate), so the stored basis stays consistent
whether or not a later run uses `--recompute-basis`.

```bash
# after editing basis_fte / basis_rev for APR-2026:
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline \
    --gl-from-db --basis-from-db --recompute-basis --period APR-2026 --to-db
```

### Closing a period

Once a month's report is final, lock it so re-runs cannot overwrite it:

```bash
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --close-period --period APR-2026 --note "final"
PYTHONPATH=. ./venv/bin/python -m cost_distribution.pipeline --reopen-period --period APR-2026
```

A closed period (a row in `period_close`) refuses every write for that period —
`--import-basis`, `--import-gl`, recompute-persist and `--to-db` snapshot loads
all fail closed until it is reopened. Reads/reports (`--dry-run`, Excel-only
runs) are unaffected, so past periods stay frozen while you keep working on the
current month.

Re-importing another month never clobbers policy edits (policy tables are only
seeded when empty, unless `--reseed-global`). Output rows and each
`distribution_run` are tagged with the `period`; a `--to-db` run replaces only
that period's snapshot. Percentages are stored `DECIMAL(30,20)` so FTE/Revenue
shares still sum to exactly 1 and the allocation ties out. Schema is
Alembic-managed under `alembic/cost/`.

**Period ties basis to GL.** The GL has no period column; the period is derived
from the `Date` (month → `MMM-YYYY`, e.g. `APR-2026`). When `--period` is given,
the GL is filtered to lines in that month, so a period's ALLOCATION/FTE/REV
factors are applied only to that period's cost lines (the run fails if no GL line
matches, warns if other-month lines are dropped). Every `distribution` row is
tagged with its own date-derived `period` the same way — so the snapshot is
self-describing by month even for a no-`--period` batch run.
