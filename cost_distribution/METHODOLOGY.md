# Automated Cost Distribution — Methodology Note

*Stakeholder-facing summary. A controller can sign off the allocation from this
page without reading code.*

## What it does

Every cost line in the **General Ledger** (`GL`, 1.804 lines for JUL-2026) is
pushed to the department(s) that should ultimately bear the cost. A line is
either:

- **charged directly** to its originating department (no distribution rule), or
- **distributed** across several *receiving* departments using a fixed set of
  split percentages.

Finally, the **administrative departments do not keep what they have collected**:
their whole accumulated cost is swept out and re-charged to the departments that
earn revenue, by headcount. This last step moves cost sideways only — it never
changes the total, and it never touches revenue.

**Revenue sits alongside cost.** Sales invoices (`sales_detail`, account group
`4xxx`) flow through `gl` into the same table, charged directly to the department
that earned them — no distribution rule applies to revenue. Their `Amount` is
`Credit − Debit`, the mirror of cost's `Debit − Credit`, so both read positive
and a department's earnings and spending can be read side by side. A credit note
stays negative, because it genuinely reduces revenue.

The result is one tidy `Distribution` table (3.047 rows) that reproduces the
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
4. **Enrich.** Each output row gets the receiving department's `Dept_div`/`PC`
   and the account's reporting line — its `Reporting Code`, name and label, all
   read from the `COA` master.
5. **Sweep the overhead.** Every department in the *Administration Indonesia*
   bucket has its **net** position reversed in full (one row at 100%) — any
   revenue it earned settles against its own cost first — and the pooled amount
   is charged to the revenue-generating departments using the `FTE - All`
   headcount shares, renormalised over the receivers and stated at 4 decimals.

> One account is routed rather than distributed: **`6924-00-000 Bank
> Administrative` goes to `Revenue & Investment Controlling` in full**, wherever
> it arises. The originating department is still recorded, so the charge stays
> traceable to its source.

The split percentages themselves trace back to headcount (`FTE`) and revenue
(`REV`) bases; `ALLOCATION` is treated as authoritative for this run.

> The sweep can be switched off with `--no-overhead` to see where cost sits
> before it is re-charged.

## Controls (the run fails closed on any breach)

| # | Control | Result |
|---|---|---|
| 1 | Grand-total tie-out (allocated = source) | **✓ variance 0.00** |
| 1b | Overhead sweep is zero-sum (moves cost, never creates it) | **✓ net 0,00** |
| 2 | Per-line conservation (Σ children = line amount) | **✓** |
| 3 | Percentage sets sum to 1 per method | ✓ (warns otherwise) |
| 4 | No orphan rules (every method has a basis) | **✓ 0 rejects** |
| 5 | Referential integrity (Dept/Code exist in masters) | ✓ misses listed |
| 6 | Row-count sanity (direct vs distributed) | **✓ 1.641 direct / 1.375 distributed** |
| 7 | Sign check (credits stay negative) | **✓ −166.507.768,03 both sides** |

Sign is preserved throughout (`Amount = Debit − Credit`); a credit distributes to
proportionally negative allocations — no absolute values are ever taken.

## Reconciliation result

*Period JUL-2026, workbook `Automated Cost Distribution V.05.xlsx`.*

| Metric | Value |
|---|---|
| Source total (GL) | **Rp 24.554.814.474,73** |
| Allocated total (Distribution) | **Rp 24.554.814.474,73** |
| Variance | **Rp 0,00** |
| GL lines / output rows | 1.804 / 3.047 |
| Direct / distributed / overhead / rejects | 1.641 / 1.375 / 31 / 0 |
| Overhead pool swept | Rp 4.019.660.640,52 |

Validated against the workbook's own `Distribution` sheet: same 3.047 rows, and
every one of the 31 `Receiving Dept` totals matches to **Rp 0,00** — including
the overhead block, which reproduces the workbook department by department.

Two columns intentionally differ from that sheet. `Reporting Code` and
`Reporting Account` are taken from the `COA` master (`012`, `200`, `360` …),
whereas the V.05 `Distribution` sheet still carries an older numbering (`503`,
`604`, `660` …) for the same reporting lines. The line *names* agree; only the
numbers differ, and no figure is affected.

## Monthly run book

Six steps close a month. `P` is the period, written `MMM-YYYY` (`YYYY-MM` is
accepted too). Every command below is prefixed with
`venv/bin/python -m cost_distribution.pipeline`.

| # | Step | Command |
|---|---|---|
| 1 | Seed the month's basis (**once**) | `--import-basis --period P` |
| 2 | *Edit `basis_fte` / `basis_rev` in SQL* | — |
| 3 | Refresh the derived percentages | `--rebuild-allocation --period P` |
| 4 | Pull the month's GL | `--import-gl-from-accounting --period P` |
| 5 | Run and store the distribution | `--period P --gl-from-db --basis-from-db --to-db` |
| 6 | Lock the month | `--close-period --period P --note "..."` |

```bash
P=AUG-2026
R="venv/bin/python -m cost_distribution.pipeline"

$R --import-basis --period $P                 # 1. roster + starting factors
#    ... edit basis_fte / basis_rev for the month ...
$R --verify-basis --period $P                 #    what is now out of step? (read-only)
$R --rebuild-allocation --period $P           # 3. recompute from the edited basis
$R --import-gl-from-accounting --period $P    # 4. cost lines from accounting.gl
$R --period $P --gl-from-db --basis-from-db --dry-run   # 5a. validate first
$R --period $P --gl-from-db --basis-from-db --to-db     # 5b. write it
$R --close-period --period $P --note "audit sign-off"   # 6. freeze
```

> **Step 1 runs once per month.** It *replaces* that period's `basis_allocation`,
> `basis_fte` and `basis_rev` from the workbook — re-running it after step 2
> discards your edits. To pick edits back up, go straight to step 3.

### Make shortcuts

Steps 3–5 are the routine ones: they are what picks up an edit to
`basis_fte`/`basis_rev` **and** new postings in `accounting.gl`. One target runs
all of them, stopping at the first failure:

```bash
make cost-update P=JUL-2026     # rebuild basis -> pull GL -> verify -> run -> store
make cost-check  P=JUL-2026     # same checks, writes nothing
make cost-seed   P=AUG-2026     # step 1, a brand-new month
make cost-close  P=JUL-2026 note="audit sign-off"
make cost-reopen P=JUL-2026
make migrate-cost               # apply pending cost_distribution_db migrations
```

### Checks and repairs

```bash
# is basis_allocation still consistent with basis_fte/basis_rev? (writes nothing,
# exits 1 when it is not — usable as a gate before closing a period)
$R --verify-basis --period $P

# validate a run without writing anything at all
$R --period $P --gl-from-db --basis-from-db --dry-run

# see where cost sits *before* the administrative overhead is swept out
$R --period $P --gl-from-db --basis-from-db --no-overhead --dry-run

# unlock a closed month (needed before any correction)
$R --reopen-period --period $P

# fall back to the workbook feed instead of accounting.gl
$R --import-gl --period $P
$R --import-basis --period $P --reseed-global   # also refresh PC/COA/LOGIC/RL
```

**Whenever `basis_fte` or `basis_rev` changes, steps 3 and 5 must both re-run.**
`basis_allocation` is a stored table, not a view: it does not follow the register
on its own, precisely so a figure cannot shift underneath a period that someone
else is working on. `--verify-basis` is what tells you the two have parted ways.

Output workbook contains `Distribution`, `Reconciliation`, and (if any) `rejects`.
Every output row carries a `gl_line_id` internally so any allocated figure traces
back to its source journal line. See `Automated_Cost_Distribution_Spec.md` for
the full functional specification.

## Editable basis in MySQL

The reference basis can be maintained in `cost_distribution_db` instead of Excel,
so it can be edited via SQL or a future app. There are two kinds:

| Kind | Tables | Period? | Changes |
|---|---|---|---|
| **Policy** | `basis_pc`, `basis_coa`, `basis_logic`, `basis_rl` | no | only on a policy change |
| **Monthly** | `basis_allocation`, `basis_fte`, `basis_rev` | `period` (MMM-YYYY) | each month |

Policy tables hold a single current version; the monthly tables carry a `period`
so every month has its own independently-editable split factors. The period
label is `MMM-YYYY` (e.g. `APR-2026`), derived from the GL date's month; the CLI
also accepts `YYYY-MM` and normalises it.

```bash
# 1. seed: policy tables once (skipped if already present), the month's factors
venv/bin/python -m cost_distribution.pipeline --import-basis --period APR-2026
#    to also refresh the policy tables from the workbook, add --reseed-global

# 2. edit the basis_* tables in MySQL as needed

# 3. run using the DB basis for that period (GL still from the workbook feed)
venv/bin/python -m cost_distribution.pipeline \
    --basis-from-db --period APR-2026 --to-db
```

### Refilling `basis_allocation` automatically

Most split factors are *derived*: change the month's headcount or revenue and the
percentages should follow. `--rebuild-allocation` recomputes them in place from
`basis_fte` / `basis_rev` and replaces that period's `basis_allocation` rows:

```bash
# edit basis_fte / basis_rev for the month, then:
venv/bin/python -m cost_distribution.pipeline \
    --rebuild-allocation --period JUL-2026
```

Every row records where its percentage came from in `basis_allocation.basis`:

| `basis` | Methods | Behaviour |
|---|---|---|
| `basis_fte` | `FTE - All`, `FTE - Head Office`, `FTE - Laboratory`, `FTE - Surabaya`, `FTE - Medan`, `Head Office : Tower G`, `Head Office : Tower B` | headcount (`HC`) share by dept within the method's scope |
| `basis_rev` | `Revenue HO`, `Revenue`, `Revenue - System Certification Services` | the matching percentage column, grouped by `Div` |
| `Fixed` | `Lab Distribution`, `Corporate Services`, `Surabaya Distribution`, `PE Product Distribution` | hand-maintained; passes through **unchanged** |

So the basis map is queryable directly, without reading any code:

```sql
SELECT basis, distribution, COUNT(*) rows, ROUND(SUM(percentage), 9) total
FROM   basis_allocation
WHERE  period = '2026-07-01'
GROUP  BY basis, distribution
ORDER  BY basis, distribution;
```

The label is derived from the method (`cost_distribution.basis.basis_of`) and
written by every path that fills the table — `--import-basis`, `--recompute-basis`
and `--rebuild-allocation` — so it can never drift from the method it describes.
A method that is neither FTE- nor revenue-driven is `Fixed` by definition.

> `Lab Distribution` sums to 31, not 1 — it is keyed on *(method × Account Name)*,
> so each of its 31 accounts sums to 1.0 across the three labs. That is correct,
> not a broken percentage set.

Two things to know:

- The **roster** of receiving departments is kept from the existing
  `basis_allocation` rows — departments that legitimately sit at 0% cannot be
  discovered from `FTE`/`REV` alone. So seed the period once with
  `--import-basis`, then refill with `--rebuild-allocation` as often as needed.
- The command reports the **max drift** against what was previously stored, and
  refuses to run on a closed period.

### Fully DB-driven month

The GL fact can also live in MySQL (`gl_entry`, period-scoped), so a whole month
runs without the workbook — the shape an app would drive:

```bash
# seed the month's GL from the workbook (only that month's lines)
venv/bin/python -m cost_distribution.pipeline --import-gl --period APR-2026

# run entirely from the database: GL + basis from MySQL, snapshot back to MySQL
venv/bin/python -m cost_distribution.pipeline \
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
venv/bin/python -m cost_distribution.pipeline \
    --gl-from-db --basis-from-db --recompute-basis --period APR-2026 --to-db
```

### Closing a period

Once a month's report is final, lock it so re-runs cannot overwrite it:

```bash
venv/bin/python -m cost_distribution.pipeline --close-period --period APR-2026 --note "final"
venv/bin/python -m cost_distribution.pipeline --reopen-period --period APR-2026
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


make sales-status     # invoice mana yang belum/kedaluwarsa (baca saja, exit 1 kalau tertinggal)
make sales-check      # validasi tanpa menulis
make sales-import FILE=sales_detail.xlsx   # impor Excel + posting sekaligus
