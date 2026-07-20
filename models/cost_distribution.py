"""
Cost Distribution Database (cost_distribution_db) — Models

Destination database for the Automated Cost Distribution engine (see
``cost_distribution/pipeline.py``). Parallel to the finance/source databases: a
separate schema holding the exploded allocation output and a per-run
reconciliation summary.

- ``distribution``     — the tidy allocated output (one row per GL line × dept)
- ``distribution_run`` — one row per pipeline run (the reconciliation controls)
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, BigInteger, String, Numeric, Date, DateTime, Text,
)
from sqlalchemy.orm import declarative_base

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
    period = Column(String(7), nullable=True, index=True)  # 'YYYY-MM'
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
    reporting_account_name = Column(String(200), nullable=True)

    dept = Column(String(200), nullable=True, index=True)
    new_dept = Column(String(200), nullable=True, index=True)
    div = Column(String(200), nullable=True, index=True)
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
    period = Column(String(7), nullable=True, index=True)  # 'YYYY-MM'

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
#     ``period`` (YYYY-MM) and each month has its own editable version.


class _RefMixin:
    """Shared columns for every basis table (no period)."""
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class _PeriodMixin(_RefMixin):
    """Basis tables scoped to a month."""
    period = Column(String(7), nullable=False, index=True)  # 'YYYY-MM'


class BasisPC(CostDistributionBase, _RefMixin):
    """Cost-/profit-centre master (workbook sheet ``PC``) — policy, no period."""
    __tablename__ = "basis_pc"
    dept_code = Column(String(50), nullable=True, index=True)
    dept = Column(String(200), nullable=True, index=True)
    div = Column(String(200), nullable=True)
    pc = Column(String(200), nullable=True, index=True)


class BasisCOA(CostDistributionBase, _RefMixin):
    """Chart of accounts (workbook sheet ``COA``) — policy, no period."""
    __tablename__ = "basis_coa"
    code = Column(String(50), nullable=True, index=True)
    account_name = Column(String(200), nullable=True)
    reporting_line = Column(String(200), nullable=True)


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
