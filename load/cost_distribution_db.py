"""Persist Automated Cost Distribution output to cost_distribution_db (MySQL).

Each run is an idempotent snapshot: the ``distribution`` table is fully replaced
and a new ``distribution_run`` reconciliation row is written. The engine is built
from ``COST_DB_*`` env vars (or ``COST_DB_URL`` if set), mirroring the finance /
source connection pattern in ``etl_pipeline.py``.
"""
from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import now_jakarta, period_to_date
from models.cost_distribution import CostDistributionBase, Distribution, DistributionRun

def get_cost_engine():
    """Build the cost_distribution_db engine (COST_DB_URL wins if provided).

    Env vars are read at call time so a ``load_dotenv`` in the caller takes
    effect regardless of import order.
    """
    url_override = os.getenv("COST_DB_URL")
    if url_override:
        return create_engine(url_override, future=True)
    cfg = {
        "host": os.getenv("COST_DB_HOST", "localhost"),
        "port": int(os.getenv("COST_DB_PORT", "3306")),
        "user": os.getenv("COST_DB_USER", "your_user"),
        "password": os.getenv("COST_DB_PASSWORD", "your_password"),
        "database": os.getenv("COST_DB_NAME", "cost_distribution_db"),
    }
    url = (
        f"mysql+mysqlconnector://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
    )
    return create_engine(url, future=True)


def get_finance_engine():
    """Build the accounting (finance) DB engine — the GL fact's upstream source.

    Same call-time env reading as ``get_cost_engine`` (and the same shape as
    ``etl_pipeline.get_finance_engine``), kept here so the cost_distribution
    package can reach accounting.gl without importing the whole ETL module.
    """
    url_override = os.getenv("FINANCE_DB_URL")
    if url_override:
        return create_engine(url_override, future=True)
    cfg = {
        "host": os.getenv("FINANCE_DB_HOST", "localhost"),
        "port": int(os.getenv("FINANCE_DB_PORT", "3306")),
        "user": os.getenv("FINANCE_DB_USER", "your_user"),
        "password": os.getenv("FINANCE_DB_PASSWORD", "your_password"),
        "database": os.getenv("FINANCE_DB_NAME", "accounting"),
    }
    url = (
        f"mysql+mysqlconnector://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
    )
    return create_engine(url, future=True)


# Map the pipeline frame's internal column names to the ORM column names. These
# are the internal names, not the workbook headers — only the written sheet takes
# the workbook's spelling (see config.OUTPUT_HEADERS), so "Div" here lands in
# ``distribution.dept_div``.
_COLUMN_MAP = {
    "Date": "date",
    "Type": "type",
    "Ref No.": "ref_no",
    "Contact": "contact",
    "Description": "description",
    "Note": "note",
    "Code": "code",
    "Account Name": "account_name",
    "Account": "account",
    "Reporting Code": "reporting_code",
    "Reporting Account Name": "reporting_account_name",
    "Reporting Account": "reporting_account",
    "Dept Code": "dept_code",
    "Dept": "dept",
    "New Dept Code": "new_dept_code",
    "New Dept": "new_dept",
    "Div": "dept_div",
    "PC": "pc",
    "Debit": "debit",
    "Credit": "credit",
    "Amount": "amount",
    "Percentage": "percentage",
    "Allocation": "allocation",
    "Distribution And Allocation": "distribution_and_allocation",
}


def create_all(engine=None) -> None:
    """Create tables if they do not exist (bootstrap outside Alembic)."""
    engine = engine or get_cost_engine()
    CostDistributionBase.metadata.create_all(engine)


def _rows_from_output(out: pd.DataFrame, run_id: int, loaded_at: datetime,
                      period: str = None):
    """Yield ORM-ready dicts from the pipeline's output frame + helper columns."""
    df = out.copy()
    # Carry method + gl_line_id from the internal helper columns if present.
    method = df["_method"] if "_method" in df.columns else pd.Series([None] * len(df))
    gl_id = df["gl_line_id"] if "gl_line_id" in df.columns else pd.Series([None] * len(df))
    # Per-row period derived from the GL date (MMM-YYYY); fall back to the run's.
    row_period = df["period"] if "period" in df.columns else pd.Series([period] * len(df))

    records = []
    for i in range(len(df)):
        row = {orm: df.iloc[i][disp] for disp, orm in _COLUMN_MAP.items() if disp in df.columns}
        # pandas NaN/NaT -> None for SQL
        row = {k: (None if pd.isna(v) else v) for k, v in row.items()}
        row["method"] = None if pd.isna(method.iloc[i]) else method.iloc[i]
        row["gl_line_id"] = None if pd.isna(gl_id.iloc[i]) else int(gl_id.iloc[i])
        row["run_id"] = run_id
        rp = row_period.iloc[i]
        p_str = period if pd.isna(rp) else rp
        # distribution.period is a DATE anchored at the 1st of the month.
        row["period"] = period_to_date(p_str)
        row["loaded_at"] = loaded_at
        records.append(row)
    return records


def load_to_db(out: pd.DataFrame, recon, cfg, recompute_basis: bool = False,
               engine=None, chunk_size: int = 1000, period: str = None) -> int:
    """Replace the distribution snapshot and record a reconciliation run.

    Returns the new ``distribution_run.id``. Everything happens in one
    transaction so a partial load cannot leave the table half-written.
    """
    engine = engine or get_cost_engine()
    create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = now_jakarta()

    with Session() as session:
        run = DistributionRun(
            run_at=now,
            period=period_to_date(period),
            source_total=recon.source_total,
            allocated_total=recon.allocated_total,
            variance=recon.variance,
            n_gl_lines=recon.n_gl_lines,
            n_output_rows=recon.n_output_rows,
            n_direct=recon.n_direct,
            n_distributed=recon.n_distributed,
            n_rejects=recon.n_rejects,
            recompute_basis=1 if recompute_basis else 0,
            input_path=getattr(cfg, "input_path", None),
        )
        session.add(run)
        session.flush()  # assign run.id
        run_id = run.id

        records = _rows_from_output(out, run_id, now, period=period)

        # Idempotent snapshot: clear exactly the periods present in this batch
        # (each period is a DATE anchored at the 1st of the month), then insert.
        periods = sorted({r["period"] for r in records if r["period"]})

        # distribution_run.period is a DATE anchored at the 1st: the requested
        # period, else the single month the output covers (None when the batch
        # spans several). `periods` already hold DATE values from _rows_from_output.
        if run.period is None and len(periods) == 1:
            run.period = periods[0]

        # Gate on the months this batch actually writes. A run invoked without
        # --period would otherwise skip the period lock entirely and overwrite a
        # closed month's snapshot; raising here rolls the whole load back.
        from load.cost_distribution_period import assert_periods_open
        assert_periods_open(session, periods, "write cost_distribution_db")

        if periods:
            session.execute(
                text("DELETE FROM distribution WHERE period IN :ps").bindparams(
                    bindparam("ps", expanding=True)
                ),
                {"ps": periods},
            )
        else:
            session.execute(text("DELETE FROM distribution WHERE period IS NULL"))

        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(Distribution, records[i:i + chunk_size])

        session.commit()
        return run_id
