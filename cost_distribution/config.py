"""Configuration for the Automated Cost Distribution pipeline.

Everything the engine needs to know that is not business logic lives here, so
there are no magic strings buried in the transforms. Override any field via the
``COST_DIST_*`` environment variables (see ``load_config``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

# Repo root (…/finance-etl-pipeline)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Config:
    # --- IO ---------------------------------------------------------------
    input_path: str = os.path.join(_ROOT, "Automated Cost Distribution V.04.xlsx")
    output_path: str = os.path.join(_ROOT, "cost_distribution", "output", "Distribution_output.xlsx")

    # --- Sheet names ------------------------------------------------------
    sheet_gl: str = "GL"
    sheet_coa: str = "COA"
    sheet_pc: str = "PC"
    sheet_logic: str = "LOGIC"
    sheet_allocation: str = "ALLOCATION"
    sheet_fte: str = "FTE"
    sheet_rev: str = "REV"
    sheet_distribution: str = "Distribution"  # expected output, used for reconciliation

    # --- Business rules ---------------------------------------------------
    # The one method whose split factors vary by account name (conditional join).
    lab_distribution_method: str = "Lab Distribution"

    # Optional basis recompute (§1.6/§1.7). FTE-* factors are the headcount (HC)
    # share by Dept within a Location scope; Revenue-* factors come from the REV
    # sheet grouped by Div. `None` scope = all employees; otherwise the Location
    # value must contain the given token (case-insensitive).
    fte_scopes: Dict[str, str] = field(default_factory=lambda: {
        "FTE - All": None,
        "FTE - Head Office": "Head Office",
        "FTE - Laboratory": "Laboratory",
        "FTE - Surabaya": "Surabaya",
        "FTE - Medan": "Medan",
    })
    rev_method_column: Dict[str, str] = field(default_factory=lambda: {
        "Revenue HO": "Percentage HO",
        "Revenue - Certification Services": "Percentage Certification Services",
        "Revenue": "Percentage All",
    })

    # --- Controls ---------------------------------------------------------
    tolerance: float = 1e-6

    # dtype hints — keep reference/code columns as text (leading zeros, slashes).
    str_columns: Dict[str, str] = field(default_factory=lambda: {
        "Ref No.": "string",
        "Account Code": "string",
        "Dept Code": "string",
        "Code": "string",
    })


def load_config() -> Config:
    """Build a Config, letting ``COST_DIST_*`` env vars override IO paths."""
    return Config(
        input_path=os.getenv("COST_DIST_INPUT", Config.input_path),
        output_path=os.getenv("COST_DIST_OUTPUT", Config.output_path),
        tolerance=float(os.getenv("COST_DIST_TOLERANCE", Config.tolerance)),
    )


# Target output column order (§4 of the spec) — 20 columns.
OUTPUT_COLUMNS = [
    "Date",
    "Type",
    "Ref No.",
    "Contact",
    "Description",
    "Note",
    "Code",
    "Account Name",
    "Account",
    "Reporting Account Name",
    "Dept",
    "New Dept",
    "Div",
    "PC",
    "Debit",
    "Credit",
    "Amount",
    "Percentage",
    "Allocation",
    "Distribution And Allocation",
]
