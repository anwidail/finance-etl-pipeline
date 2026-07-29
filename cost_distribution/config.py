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
    input_path: str = os.path.join(_ROOT, "Automated Cost Distribution V.05.xlsx")
    output_path: str = os.path.join(_ROOT, "cost_distribution", "output", "Distribution_output.xlsx")

    # --- Sheet names ------------------------------------------------------
    sheet_gl: str = "GL"
    sheet_coa: str = "COA"
    sheet_pc: str = "PC"
    sheet_logic: str = "LOGIC"
    sheet_allocation: str = "ALLOCATION"
    sheet_fte: str = "FTE"
    sheet_rev: str = "REV"
    sheet_rl: str = "RL"  # reporting-line master (V.05)
    sheet_distribution: str = "Distribution"  # expected output, used for reconciliation

    # --- Business rules ---------------------------------------------------
    # The one method whose split factors vary by account name (conditional join).
    lab_distribution_method: str = "Lab Distribution"

    # Optional basis recompute (§1.6/§1.7). FTE-driven factors are the headcount
    # (HC) share by Dept within a scope, taken from the FTE register; Revenue-
    # driven factors come from the REV sheet grouped by Div. Every method not
    # listed in either map is "Fixed" — a hand-maintained split that passes
    # through the recompute untouched (Lab Distribution, Corporate Services,
    # Surabaya Distribution, PE Product Distribution).
    #
    # Each scope is (FTE column to filter on, substring to match, case-insensitive).
    # `(None, None)` means every employee. The Tower splits scope on
    # `Location Detail` — `Location` only goes down to "Head Office" and cannot
    # tell the two towers apart.
    fte_scopes: Dict[str, tuple] = field(default_factory=lambda: {
        "FTE - All": (None, None),
        "FTE - Head Office": ("Location", "Head Office"),
        "FTE - Laboratory": ("Location", "Laboratory"),
        "FTE - Surabaya": ("Location", "Surabaya"),
        "FTE - Medan": ("Location", "Medan"),
        "Head Office : Tower G": ("Location Detail", "Head Office : Tower G"),
        "Head Office : Tower B": ("Location Detail", "Head Office : Tower B"),
    })
    rev_method_column: Dict[str, str] = field(default_factory=lambda: {
        "Revenue HO": "Percentage HO",
        "Revenue - System Certification Services": "Percentage Certification Services",
        "Revenue": "Percentage All",
    })

    # --- Overhead sweep (§2.4, new in V.05) -------------------------------
    # After the normal distribution, the administrative departments' accumulated
    # cost is swept out (a negative row per dept, at 100%) and pushed onto the
    # revenue-generating departments by headcount. The block is zero-sum, so the
    # grand total is unchanged.
    overhead_enabled: bool = True
    # Departments to sweep are identified by their PC bucket in the PC master.
    overhead_sweep_pc: str = "Administration Indonesia"
    # Receiving depts + weights: this ALLOCATION method's roster, minus the
    # swept depts, renormalised over what remains.
    overhead_basis_method: str = "FTE - All"
    # The workbook states the receiving shares at 4 decimals; matching that is
    # what ties the sweep back to the workbook to the cent.
    overhead_pct_decimals: int = 4
    # COA account the sweep is booked to (carries its own reporting line).
    overhead_account_code: str = "7501-00-000"
    overhead_account_name: str = "Allocation Overhead"

    # --- GL sourced from accounting.gl (--import-gl-from-accounting) ------
    # The distribution covers revenue (4xxx) and cost (5 cost of sales,
    # 6 operating expense, 7 depreciation/allocation). Still excluded is
    # non-operating income/expense — 8xxx forex/other income and 9xxx
    # interest/forex loss/other expense.
    gl_reporting_group: str = "Profit & Loss"
    gl_account_prefixes: tuple = ("4", "5", "6", "7")
    # Accounts whose natural balance is a credit. Their Amount is Credit − Debit,
    # so revenue reads as a positive figure of its own rather than as negative
    # cost — the distribution then shows what each department earned and spent,
    # each in its own sign. Everything else stays Debit − Credit.
    credit_balance_prefixes: tuple = ("4",)
    gl_status_allowed: tuple = ("approved",)
    # accounting.gl still carries pre-V.05 department names; canonicalise on
    # import so gl_entry always speaks the current PC master's vocabulary.
    # Only the workbook feed still needs these: rows coming from accounting are
    # already canonical, because `accounting.department` is applied there by
    # dept_code (see load.department_loader.canonicalise_names).
    #
    # Both follow the master rather than a guess from the old wording: D015 is
    # "Automotive" and D01 is "System Certification Services". An earlier version
    # of this map sent CERTIFICATION SERVICES to "Integrated Management System",
    # which is D010 — a different department.
    dept_aliases: Dict[str, str] = field(default_factory=lambda: {
        "Automotive (System Certification Services Technical Ops)": "Automotive",
        "CERTIFICATION SERVICES": "System Certification Services",
    })
    # Same for account names. The LOGIC rule key includes the account name (one
    # account code can carry different methods per name — see build_lookups), so
    # a stale name silently demotes a distributed line to a direct charge. These
    # are renames V.05 made that accounting's own COA has not picked up.
    # NOT derivable from the COA sheet: COA itself still holds the old name here.
    account_name_aliases: Dict[str, str] = field(default_factory=lambda: {
        "Internal Events, Meeting and Briefing": "Internal Events",
    })

    # --- Controls ---------------------------------------------------------
    tolerance: float = 1e-6
    # Grand-total tie-out is also allowed |source| * rel_tolerance, because
    # float64 summation noise scales with the magnitude of the total (a ~2.5e10
    # total accumulates ~1e-5 of noise, far below a cent but above 1e-6).
    rel_tolerance: float = 1e-13

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


# Target output column order (§4 of the spec) — 22 columns as of V.05, which
# added Reporting Code / Reporting Account and renamed Div -> Dept_div.
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
    "Reporting Code",
    "Reporting Account Name",
    "Reporting Account",
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

# Internal column name -> V.05 workbook header. The pipeline keeps the stable
# internal names end to end; only the written sheet takes the workbook's
# spelling, so the DB mapping and every transform stay on one vocabulary.
OUTPUT_HEADERS = {
    "Date": "date",
    "Div": "Dept_div",
}
