"""Persist Automated Cost Distribution output to cost_distribution_db (MySQL).

Each run is an idempotent snapshot: the ``distribution`` table is fully replaced
and a new ``distribution_run`` reconciliation row is written. The engine is built
from ``COST_DB_*`` env vars (or ``COST_DB_URL`` if set), mirroring the finance /
source connection pattern in ``etl_pipeline.py``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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


# Map the tidy output frame's display columns to the ORM column names.
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
    "Reporting Account Name": "reporting_account_name",
    "Dept": "dept",
    "New Dept": "new_dept",
    "Div": "div",
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

    records = []
    for i in range(len(df)):
        row = {orm: df.iloc[i][disp] for disp, orm in _COLUMN_MAP.items() if disp in df.columns}
        # pandas NaN/NaT -> None for SQL
        row = {k: (None if pd.isna(v) else v) for k, v in row.items()}
        row["method"] = None if pd.isna(method.iloc[i]) else method.iloc[i]
        row["gl_line_id"] = None if pd.isna(gl_id.iloc[i]) else int(gl_id.iloc[i])
        row["run_id"] = run_id
        row["period"] = period
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
    now = datetime.now(timezone.utc)

    with Session() as session:
        run = DistributionRun(
            run_at=now,
            period=period,
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

        # Idempotent snapshot: clear this period's rows (or the whole table when
        # no period is given), then insert this run's rows.
        if period:
            session.execute(text("DELETE FROM distribution WHERE period = :p"), {"p": period})
        else:
            session.execute(text("DELETE FROM distribution WHERE period IS NULL"))
        records = _rows_from_output(out, run_id, now, period=period)
        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(Distribution, records[i:i + chunk_size])

        session.commit()
        return run_id
