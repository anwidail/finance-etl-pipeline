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

from cost_distribution.periods import now_jakarta, period_to_date
from models.cost_distribution import (
    BasisPC, BasisCOA, BasisLogic, BasisAllocation, BasisFTE, BasisREV, BasisRL,
)

# Policy tables are global (no period, change only on a policy change); the rest
# are month-scoped. Kept in sync with the mixins in models.cost_distribution.
GLOBAL_SHEETS = {"PC", "COA", "LOGIC", "RL"}

# Reference-only sheets: nothing in the pipeline reads them, so a DB seeded
# before they existed still runs --basis-from-db instead of failing closed.
OPTIONAL_SHEETS = {"RL"}

# Per table: ORM class + {orm_attr: workbook_sheet_column}. The sheet-column
# names are what the pipeline (build_lookups / basis recompute) expects, so the
# round-trip Excel -> DB -> DataFrame is lossless.
BASIS_TABLES = {
    "PC": (BasisPC, {
        "dept_code": "Dept Code", "dept": "Dept", "div": "Div", "pc": "PC",
    }),
    "COA": (BasisCOA, {
        "code": "Code", "account_name": "Account Name",
        "reporting_code": "Reporting Code", "reporting_line": "Reporting Line",
        "reporting_account": "Reporting Account",
    }),
    "RL": (BasisRL, {
        "head_code": "Head Code", "head_description": "Head Description",
        # The workbook's header carries a trailing newline; normalised on import.
        "reporting_line": "Reporting Line", "reporting_line_name": "reporting_line_name",
        "reporting_code": "reporting_code", "description": "Description",
    }),
    "LOGIC": (BasisLogic, {
        "account_code": "Account Code", "account_name": "Account Name",
        "pc": "PC", "distribution": "Distribution", "code": "Code",
    }),
    "ALLOCATION": (BasisAllocation, {
        "distribution": "Distribution", "basis": "Basis",
        "account_name": "Account Name",
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


def import_basis_from_workbook(cfg, period: str, engine, chunk_size: int = 1000,
                               refresh_global: bool = False) -> Dict[str, int]:
    """Seed the basis_* tables from the workbook. Returns row counts per sheet.

    - Month-scoped tables (ALLOCATION, FTE, REV) always replace this ``period``'s
      rows (idempotent per month).
    - Policy tables (PC, COA, LOGIC) are global: seeded only when empty, so
      re-importing another month never clobbers policy edits. Pass
      ``refresh_global=True`` to force a full refresh from the workbook.
    """
    from load.cost_distribution_db import create_all
    create_all(engine)

    period_date = period_to_date(period)  # month-scoped tables store a DATE
    xl = pd.ExcelFile(cfg.input_path, engine="openpyxl")
    Session = sessionmaker(bind=engine, future=True)
    now = now_jakarta()
    counts: Dict[str, int] = {}

    with Session() as session:
        for sheet, (model, colmap) in BASIS_TABLES.items():
            is_global = sheet in GLOBAL_SHEETS

            if is_global:
                if session.query(model).count() and not refresh_global:
                    counts[sheet] = 0  # left as-is (already seeded)
                    continue
                session.execute(delete(model))
            else:
                session.execute(delete(model).where(model.period == period_date))

            if sheet not in xl.sheet_names:
                if sheet in OPTIONAL_SHEETS:
                    counts[sheet] = 0
                    continue
                raise ValueError(f"Workbook {cfg.input_path!r} has no sheet {sheet!r}")
            df = xl.parse(sheet)
            # Some headers carry stray whitespace/newlines (RL's "Reporting Line\n").
            df.columns = [str(c).strip() for c in df.columns]
            # The workbook has no Basis column — derive it from the method so the
            # stored basis records where each percentage comes from.
            if sheet == "ALLOCATION" and "Basis" not in df.columns:
                from cost_distribution.basis import basis_column
                df["Basis"] = basis_column(cfg, df["Distribution"])
            # The workbook's COA sheet still carries a few pre-V.05 account
            # names that LOGIC/ALLOCATION have already moved on from. Canonicalise
            # so one account code never means two names across the basis tables.
            if sheet == "COA" and "Account Name" in df.columns:
                df["Account Name"] = (df["Account Name"].astype("string").str.strip()
                                      .replace(cfg.account_name_aliases))
            records = []
            for _, row in df.iterrows():
                rec = {orm: _clean(row[src]) for orm, src in colmap.items() if src in df.columns}
                if not is_global:
                    rec["period"] = period_date
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
    period_date = period_to_date(period)  # month-scoped tables store a DATE
    Session = sessionmaker(bind=engine, future=True)
    out: Dict[str, pd.DataFrame] = {}

    with Session() as session:
        for sheet, (model, colmap) in BASIS_TABLES.items():
            if sheet in GLOBAL_SHEETS:
                rows = session.query(model).all()  # policy: single global version
                where = "(global)"
            else:
                rows = session.query(model).filter(model.period == period_date).all()
                where = f"period {period!r}"
            if not rows:
                if sheet in OPTIONAL_SHEETS:
                    out[sheet] = pd.DataFrame(columns=list(colmap.values()))
                    continue
                raise ValueError(
                    f"No basis rows for sheet {sheet!r} at {where}. "
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


def persist_allocation(period: str, refreshed_alloc: pd.DataFrame, engine,
                       chunk_size: int = 1000, cfg=None) -> int:
    """Replace ``basis_allocation`` for ``period`` with a refreshed ALLOCATION.

    Used to write recomputed FTE-*/Revenue-* factors back so the stored basis
    stays consistent (a full replace of that period's rows — no duplicate rows
    accumulate on repeated recomputes). Returns the row count written.

    ``recompute_allocation`` already stamps the ``Basis`` column; pass ``cfg`` to
    have it derived here for a frame that lacks it.
    """
    df = refreshed_alloc.copy()
    if "Basis" not in df.columns:
        from cost_distribution.basis import basis_column
        from cost_distribution.config import load_config
        df["Basis"] = basis_column(cfg or load_config(), df["Distribution"])
    period_date = period_to_date(period)  # basis_allocation.period is a DATE
    Session = sessionmaker(bind=engine, future=True)
    now = now_jakarta()
    with Session() as session:
        session.execute(delete(BasisAllocation).where(BasisAllocation.period == period_date))
        records = []
        for _, row in df.iterrows():
            records.append({
                "distribution": _clean(row.get("Distribution")),
                "basis": _clean(row.get("Basis")),
                "account_name": _clean(row.get("Account Name")),
                "new_dept": _clean(row.get("New Dept")),
                "percentage": _clean(row.get("Percentage")),
                "period": period_date,
                "created_at": now,
                "updated_at": now,
            })
        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(BasisAllocation, records[i:i + chunk_size])
        session.commit()
    return len(records)
