"""Editable monthly basis in MySQL for the cost distribution engine.

The reference sheets (PC, COA, LOGIC, ALLOCATION, FTE, REV) are mirrored into
period-scoped MySQL tables so the distribution basis can be maintained per month
(and later via an app) instead of editing Excel:

- ``import_basis_from_workbook`` seeds a period's basis from the workbook.
- ``read_basis_from_db`` returns DataFrames shaped exactly like the workbook
  sheets, so the pipeline consumes them unchanged (``--basis-from-db``).

Each import fully replaces that period's rows, so re-seeding is idempotent and
never mixes months.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from models.cost_distribution import (
    BasisPC, BasisCOA, BasisLogic, BasisAllocation, BasisFTE, BasisREV,
)

# Per table: ORM class + {orm_attr: workbook_sheet_column}. The sheet-column
# names are what the pipeline (build_lookups / basis recompute) expects, so the
# round-trip Excel -> DB -> DataFrame is lossless.
BASIS_TABLES = {
    "PC": (BasisPC, {
        "dept_code": "Dept Code", "dept": "Dept", "div": "Div", "pc": "PC",
    }),
    "COA": (BasisCOA, {
        "code": "Code", "account_name": "Account Name",
        "reporting_line": "Reporting Line",
    }),
    "LOGIC": (BasisLogic, {
        "account_code": "Account Code", "account_name": "Account Name",
        "pc": "PC", "distribution": "Distribution", "code": "Code",
    }),
    "ALLOCATION": (BasisAllocation, {
        "distribution": "Distribution", "account_name": "Account Name",
        "new_dept": "New Dept", "percentage": "Percentage",
    }),
    "FTE": (BasisFTE, {
        "fte": "FTE", "hc": "HC", "name": "Name", "employee_no": "Employee_No.",
        "dept": "Dept", "div": "Div", "pc": "PC", "location": "Location",
        "location_detail": "Location Detail",
    }),
    "REV": (BasisREV, {
        "div": "Div", "pc": "PC", "location": "Location", "amount": "Amount",
        "pct_certification_services": "Percentage Certification Services",
        "pct_ho": "Percentage HO", "pct_all": "Percentage All",
    }),
}


def _clean(value):
    return None if pd.isna(value) else value


def import_basis_from_workbook(cfg, period: str, engine, chunk_size: int = 1000) -> Dict[str, int]:
    """Seed the basis_* tables for ``period`` from the workbook. Returns row counts.

    Replaces any existing rows for that period first (idempotent per month).
    """
    from load.cost_distribution_db import create_all
    create_all(engine)

    xl = pd.ExcelFile(cfg.input_path, engine="openpyxl")
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc)
    counts: Dict[str, int] = {}

    with Session() as session:
        for sheet, (model, colmap) in BASIS_TABLES.items():
            df = xl.parse(sheet)
            session.execute(delete(model).where(model.period == period))

            records = []
            for _, row in df.iterrows():
                rec = {orm: _clean(row[src]) for orm, src in colmap.items() if src in df.columns}
                rec["period"] = period
                rec["created_at"] = now
                rec["updated_at"] = now
                records.append(rec)
            for i in range(0, len(records), chunk_size):
                session.bulk_insert_mappings(model, records[i:i + chunk_size])
            counts[sheet] = len(records)
        session.commit()
    return counts


def read_basis_from_db(period: str, engine) -> Dict[str, pd.DataFrame]:
    """Return workbook-shaped DataFrames for ``period`` (PC/COA/LOGIC/ALLOCATION/FTE/REV).

    Raises if the period has no basis rows for a required sheet.
    """
    Session = sessionmaker(bind=engine, future=True)
    out: Dict[str, pd.DataFrame] = {}

    with Session() as session:
        for sheet, (model, colmap) in BASIS_TABLES.items():
            rows = session.query(model).filter(model.period == period).all()
            if not rows:
                raise ValueError(
                    f"No basis rows for sheet {sheet!r} at period {period!r}. "
                    f"Seed it first with import_basis_from_workbook / --import-basis."
                )
            # Cast Decimal -> float so downstream pandas arithmetic (Amount *
            # Percentage, HC shares) never mixes Decimal with float.
            data = [
                {src: (float(v) if isinstance(v, Decimal) else v)
                 for orm, src in colmap.items() for v in (getattr(r, orm),)}
                for r in rows
            ]
            out[sheet] = pd.DataFrame(data, columns=list(colmap.values()))
    return out
