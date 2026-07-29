"""
Cost Distribution Database (cost_distribution_db) — Models

Destination database for the Automated Cost Distribution engine (see
``cost_distribution/pipeline.py``). Parallel to the finance/source databases: a
separate schema holding the exploded allocation output and a per-run
reconciliation summary.

- ``distribution``     — the tidy allocated output (one row per GL line × dept)
- ``distribution_run`` — one row per pipeline run (the reconciliation controls)
"""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Numeric, Date, DateTime, Text, func,
)
from sqlalchemy.orm import declarative_base

from cost_distribution.periods import now_jakarta

CostDistributionBase = declarative_base()


class Distribution(CostDistributionBase):
    """Allocated cost rows — mirrors the workbook's ``Distribution`` sheet.

    ``gl_line_id`` is the stable source-line identifier carried through the
    pipeline, so any allocated figure traces back to its GL journal line.
    Each pipeline run fully replaces this table (idempotent snapshot);
    ``run_id`` ties every row to its ``distribution_run`` record.
    """

    __tablename__ = "distribution"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=True, index=True)
    period = Column(Date, nullable=True, index=True)  # month anchored at the 1st, e.g. 2026-04-01
    gl_line_id = Column(Integer, nullable=True, index=True)

    date = Column(Date, nullable=True, index=True)
    type = Column(String(50), nullable=True, index=True)
    ref_no = Column(String(100), nullable=True, index=True)
    contact = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    note = Column(Text, nullable=True)

    code = Column(String(50), nullable=True, index=True)
    account_name = Column(String(200), nullable=True)
    account = Column(String(260), nullable=True)
    # Reporting line, its code and the "<code> <line>" label — all from COA.
    reporting_code = Column(String(50), nullable=True, index=True)
    reporting_account_name = Column(String(200), nullable=True)
    reporting_account = Column(String(260), nullable=True)

    # Both departments carry their code as well as their name: the code is the
    # reference, the name a label that renames out from under you. `dept_code`
    # comes from the GL line; `new_dept_code` is resolved from the PC master,
    # because the receiving department is chosen by ALLOCATION, which is
    # authored by name.
    dept_code = Column(String(50), nullable=True, index=True)
    dept = Column(String(200), nullable=True, index=True)
    new_dept_code = Column(String(50), nullable=True, index=True)
    new_dept = Column(String(200), nullable=True, index=True)
    dept_div = Column(String(200), nullable=True, index=True)
    pc = Column(String(200), nullable=True, index=True)

    debit = Column(Numeric(18, 2), nullable=False, default=0)
    credit = Column(Numeric(18, 2), nullable=False, default=0)
    amount = Column(Numeric(18, 2), nullable=False, default=0)
    percentage = Column(Numeric(18, 9), nullable=False, default=0)
    allocation = Column(Numeric(18, 2), nullable=False, default=0)

    method = Column(String(100), nullable=True, index=True)
    distribution_and_allocation = Column(String(300), nullable=True)

    loaded_at = Column(DateTime, nullable=True)


class DistributionRun(CostDistributionBase):
    """One row per pipeline run — the reconciliation controls (§5)."""

    __tablename__ = "distribution_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime, nullable=True, index=True)
    period = Column(Date, nullable=True, index=True)  # month anchored at the 1st, e.g. 2026-04-01

    source_total = Column(Numeric(20, 2), nullable=True)
    allocated_total = Column(Numeric(20, 2), nullable=True)
    variance = Column(Numeric(20, 6), nullable=True)

    n_gl_lines = Column(Integer, nullable=True)
    n_output_rows = Column(Integer, nullable=True)
    n_direct = Column(Integer, nullable=True)
    n_distributed = Column(Integer, nullable=True)
    n_rejects = Column(Integer, nullable=True)

    recompute_basis = Column(Integer, nullable=True)  # 0/1 flag
    input_path = Column(String(500), nullable=True)


# ---------------------------------------------------------------------------
# Editable monthly basis tables
# ---------------------------------------------------------------------------
# These mirror the workbook's reference sheets so the distribution basis can be
# maintained directly in MySQL — edited via SQL or a future app — instead of
# editing Excel. Seed them with
# ``load.cost_distribution_basis.import_basis_from_workbook`` and run the
# pipeline with ``--basis-from-db --period YYYY-MM``.
#
# Two kinds of basis:
#   * Policy tables (PC, COA, LOGIC) — global, change only on a policy change,
#     so they carry no ``period`` (one current version).
#   * Monthly tables (ALLOCATION, FTE, REV) — the split factors and their
#     headcount/revenue drivers change each month, so every row carries a
#     ``period`` (a DATE anchored at the 1st of the month) and each month has
#     its own editable version.


class _RefMixin:
    """Shared columns for every basis table (no period).

    ``created_at``/``updated_at`` are maintained by the *database*, so a row
    edited by hand in SQL is stamped exactly like one written by the loaders:

    - insert  -> column default
    - update  -> a ``BEFORE UPDATE`` trigger (see the Alembic revision)

    All stamps are **Jakarta local time (WIB, UTC+7)** — the clock
    ``periods.now_jakarta`` returns, which the loaders use for every write. MySQL
    gets ``UTC_TIMESTAMP() + INTERVAL 7 HOUR`` from the migration rather than
    ``CURRENT_TIMESTAMP`` so the value is WIB regardless of the database host's
    own timezone.

    The Python-side ``default``/``onupdate`` below keep the same clock on
    backends without the trigger (the test suite's SQLite, or a bare
    ``create_all`` bootstrap); the migration is the authoritative schema.
    """
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=now_jakarta,
                        server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=now_jakarta,
                        onupdate=now_jakarta, server_default=func.now())


class _PeriodMixin(_RefMixin):
    """Basis tables scoped to a month."""
    period = Column(Date, nullable=False, index=True)  # month anchored at the 1st, e.g. 2026-04-01


class BasisPC(CostDistributionBase, _RefMixin):
    """Cost-/profit-centre master (workbook sheet ``PC``) — policy, no period."""
    __tablename__ = "basis_pc"
    dept_code = Column(String(50), nullable=True, index=True)
    dept = Column(String(200), nullable=True, index=True)
    div = Column(String(200), nullable=True)
    pc = Column(String(200), nullable=True, index=True)


class BasisCOA(CostDistributionBase, _RefMixin):
    """Chart of accounts (workbook sheet ``COA``) — policy, no period.

    Each account resolves to a reporting line: ``reporting_code`` is the line's
    id in the ``basis_rl`` master, ``reporting_line`` its name, and
    ``reporting_account`` the "<code> <name>" label the output carries.
    """
    __tablename__ = "basis_coa"
    code = Column(String(50), nullable=True, index=True)
    account_name = Column(String(200), nullable=True)
    reporting_code = Column(String(50), nullable=True, index=True)
    reporting_line = Column(String(200), nullable=True)
    reporting_account = Column(String(260), nullable=True)


class BasisRL(CostDistributionBase, _RefMixin):
    """Reporting-line master (workbook sheet ``RL``) — policy, no period.

    The group reporting hierarchy each COA account rolls up to: a head
    (``head_code``/``head_description``) containing reporting lines. Reference
    data — the pipeline reads the resolved line off COA — kept here so the
    hierarchy is queryable alongside the rest of the basis.
    """
    __tablename__ = "basis_rl"
    head_code = Column(String(50), nullable=True, index=True)
    head_description = Column(String(200), nullable=True)
    reporting_line = Column(String(50), nullable=True, index=True)
    reporting_line_name = Column(String(200), nullable=True)
    reporting_code = Column(String(50), nullable=True, index=True)
    description = Column(String(300), nullable=True)


class BasisLogic(CostDistributionBase, _RefMixin):
    """Distribution rule table (workbook sheet ``LOGIC``) — policy, no period.

    Rule key is (account_code, account_name, pc-bucket) -> distribution method.
    """
    __tablename__ = "basis_logic"
    account_code = Column(String(50), nullable=True, index=True)
    account_name = Column(String(200), nullable=True, index=True)
    pc = Column(String(200), nullable=True, index=True)          # bucket
    distribution = Column(String(100), nullable=True, index=True)  # method
    code = Column(String(300), nullable=True)                    # descriptive id


class BasisAllocation(CostDistributionBase, _PeriodMixin):
    """Split-factor table (workbook sheet ``ALLOCATION``).

    ``account_name`` is only populated for the ``Lab Distribution`` method.
    """
    __tablename__ = "basis_allocation"
    distribution = Column(String(100), nullable=True, index=True)  # method
    # Where this row's percentage comes from: 'basis_fte' (headcount share),
    # 'basis_rev' (revenue share) or 'Fixed' (hand-maintained, never recomputed).
    # Derived from the method — see cost_distribution.basis.basis_of.
    basis = Column(String(20), nullable=True, index=True)
    account_name = Column(String(200), nullable=True, index=True)
    new_dept = Column(String(200), nullable=True, index=True)
    # High precision: FTE/Revenue shares are repeating decimals that must sum to
    # exactly 1 per method, or the allocation no longer ties out to source.
    percentage = Column(Numeric(30, 20), nullable=True)


class BasisFTE(CostDistributionBase, _PeriodMixin):
    """Headcount register (workbook sheet ``FTE``) — basis for FTE-* recompute."""
    __tablename__ = "basis_fte"
    fte = Column(Numeric(18, 6), nullable=True)
    hc = Column(Numeric(18, 6), nullable=True)
    name = Column(String(200), nullable=True)
    employee_no = Column(String(50), nullable=True, index=True)
    dept = Column(String(200), nullable=True, index=True)
    div = Column(String(200), nullable=True)
    pc = Column(String(200), nullable=True)
    location = Column(String(200), nullable=True, index=True)
    location_detail = Column(String(200), nullable=True)


class BasisREV(CostDistributionBase, _PeriodMixin):
    """Revenue basis (workbook sheet ``REV``) — basis for Revenue-* recompute."""
    __tablename__ = "basis_rev"
    div = Column(String(200), nullable=True, index=True)
    pc = Column(String(200), nullable=True)
    location = Column(String(200), nullable=True)
    amount = Column(Numeric(20, 2), nullable=True)
    pct_certification_services = Column(Numeric(30, 20), nullable=True)
    pct_ho = Column(Numeric(30, 20), nullable=True)
    pct_all = Column(Numeric(30, 20), nullable=True)


class GLEntry(CostDistributionBase, _PeriodMixin):
    """General ledger cost lines (workbook sheet ``GL``) — the monthly fact.

    Period-scoped so a whole month can run fully from MySQL (``--gl-from-db``)
    instead of the workbook feed. Mirrors the GL sheet's columns 1:1.
    """
    __tablename__ = "gl_entry"
    date = Column(Date, nullable=True, index=True)
    type = Column(String(50), nullable=True)
    ref_no = Column(String(100), nullable=True, index=True)
    contact = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    # Department as the source states it: the code is the stable key, the name
    # only a label. Nullable because not every feed carries one (sales_detail).
    dept_code = Column(String(50), nullable=True, index=True)
    dept = Column(String(200), nullable=True, index=True)
    project = Column(String(200), nullable=True)
    curr = Column(String(10), nullable=True)
    # High precision: source Debit/Credit carry up to ~11 decimals (FX-converted
    # lines); storing at (18,2) would shift the source total off the workbook.
    debit = Column(Numeric(28, 12), nullable=True)
    credit = Column(Numeric(28, 12), nullable=True)
    balance = Column(Numeric(28, 12), nullable=True)
    account_code = Column(String(50), nullable=True, index=True)
    account_name = Column(String(200), nullable=True, index=True)
    note2 = Column(Text, nullable=True)  # reviewer annotation column (V.05)


class PeriodClose(CostDistributionBase):
    """Period lock — a closed period is frozen against any further writes.

    Presence of a row with ``status='closed'`` means the period's GL, basis and
    distribution snapshot are locked: seeding, recompute-persist and snapshot
    loads for it are refused until it is reopened. Reads/reports are unaffected.
    """

    __tablename__ = "period_close"
    period = Column(Date, primary_key=True)  # month anchored at the 1st, e.g. 2026-04-01
    status = Column(String(10), nullable=False, default="closed")  # closed
    closed_at = Column(DateTime, nullable=True)
    note = Column(String(300), nullable=True)
