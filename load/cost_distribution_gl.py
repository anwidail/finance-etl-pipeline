"""GL fact in MySQL — import the monthly cost lines and read them back.

Lets a whole month run fully from the database: seed a period's GL with
``import_gl_from_workbook`` and run the pipeline with ``--gl-from-db``. The GL
has no period column of its own; each row's period is derived from its date
(MMM-YYYY), the same rule the pipeline uses.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import date_to_period
from models.cost_distribution import GLEntry

# ORM attr -> workbook GL sheet column. Sheet-column names match what the
# pipeline expects, so Excel -> DB -> DataFrame is lossless.
GL_COLUMNS = {
    "date": "Date",
    "type": "Type",
    "ref_no": "Ref No.",
    "contact": "Contact",
    "description": "Description",
    "note": "Note",
    "dept": "Dept",
    "project": "Project",
    "curr": "Curr",
    "debit": "Debit",
    "credit": "Credit",
    "balance": "Balance",
    "account_code": "Account Code",
    "account_name": "Account Name",
}


def _clean(value):
    return None if pd.isna(value) else value


def import_gl_from_workbook(cfg, period: str, engine, chunk_size: int = 1000) -> int:
    """Seed ``gl_entry`` for ``period`` from the workbook GL sheet. Returns row count.

    Only GL lines whose date falls in ``period`` are imported, and that period's
    rows are replaced first (idempotent per month).
    """
    from load.cost_distribution_db import create_all
    create_all(engine)

    df = pd.read_excel(cfg.input_path, sheet_name=cfg.sheet_gl,
                       dtype={"Ref No.": "string", "Account Code": "string"},
                       engine="openpyxl")
    row_period = pd.to_datetime(df["Date"], errors="coerce").apply(date_to_period)
    df = df[row_period == period].copy()

    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc)
    with Session() as session:
        session.execute(delete(GLEntry).where(GLEntry.period == period))
        records = []
        for _, row in df.iterrows():
            rec = {orm: _clean(row[src]) for orm, src in GL_COLUMNS.items() if src in df.columns}
            # date -> python date
            d = pd.to_datetime(rec.get("date"), errors="coerce")
            rec["date"] = None if pd.isna(d) else d.date()
            rec["period"] = period
            rec["created_at"] = now
            rec["updated_at"] = now
            records.append(rec)
        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(GLEntry, records[i:i + chunk_size])
        session.commit()
    return len(records)


def read_gl_from_db(period: str, engine) -> pd.DataFrame:
    """Return a workbook-shaped GL DataFrame for ``period`` (Decimal cast to float)."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        rows = session.query(GLEntry).filter(GLEntry.period == period).all()
        if not rows:
            raise ValueError(
                f"No GL rows at period {period!r}. Seed them first with "
                f"import_gl_from_workbook / --import-gl."
            )
        data = [
            {src: (float(v) if isinstance(v, Decimal) else v)
             for orm, src in GL_COLUMNS.items() for v in (getattr(r, orm),)}
            for r in rows
        ]
    return pd.DataFrame(data, columns=list(GL_COLUMNS.values()))
