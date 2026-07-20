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
