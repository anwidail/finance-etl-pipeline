"""Automated Cost Distribution — pandas ETL engine.

Reproduces the workbook's ``Distribution`` sheet: every General Ledger cost line
is either charged directly to its originating department or exploded across
receiving departments, driven by the ``LOGIC`` rule table and the ``ALLOCATION``
basis table. The pipeline is parameterised (see ``config.py``), idempotent, and
self-reconciling — total allocated cost ties out to total source cost to the
cent, or the run fails closed.

Pipeline (ordered): extract → build_lookups → resolve_rule → distribute →
enrich → validate → load. Run ``python -m cost_distribution.pipeline --dry-run``
to validate without writing.

Grain: input = one GL journal line; output = one (GL line × receiving dept) row.
A stable ``gl_line_id`` is carried from source to every child row so any
allocated figure traces back to its journal line.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd

from cost_distribution.config import Config, OUTPUT_COLUMNS, load_config

logger = logging.getLogger("cost_distribution")

# Internal-only helper columns, stripped before writing the final frame.
_BUCKET = "_bucket"
_METHOD = "_method"
_GL_ID = "gl_line_id"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def _strip(series: pd.Series) -> pd.Series:
    """Trim whitespace on a string key without disturbing non-string dtypes."""
    return series.astype("string").str.strip()


def _norm(series: pd.Series) -> pd.Series:
    """Normalised join key: trimmed + collapsed internal whitespace, casefolded.

    Used only for *matching* (Dept, Account Name); the original display value is
    preserved in the output columns.
    """
    s = series.astype("string").str.strip()
    s = s.str.replace(r"\s+", " ", regex=True)
    return s.str.casefold()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
@dataclass
class Lookups:
    dept_to_bucket: Dict[str, str]          # norm(Dept) -> PC bucket
    dept_to_div: Dict[str, str]             # norm(Dept) -> Div
    dept_to_pc: Dict[str, str]              # norm(Dept) -> PC
    code_to_reporting: Dict[str, str]       # Account Code -> Reporting Line
    rule: pd.DataFrame                      # LOGIC with norm keys
    alloc_lab: pd.DataFrame                 # ALLOCATION rows for Lab Distribution
    alloc_other: pd.DataFrame               # ALLOCATION rows for all other methods
    pc_depts: set                           # norm(Dept) present in PC
    coa_codes: set                          # Account Code present in COA


# ---------------------------------------------------------------------------
# 1. Extract
# ---------------------------------------------------------------------------
def extract(cfg: Config) -> Dict[str, pd.DataFrame]:
    """Read every required sheet; keep ref/code columns as text."""
    xl = pd.ExcelFile(cfg.input_path, engine="openpyxl")
    dtypes = cfg.str_columns

    def read(sheet: str) -> pd.DataFrame:
        use = {c: t for c, t in dtypes.items()}
        return xl.parse(sheet, dtype=use)

    sheets = {
        "GL": read(cfg.sheet_gl),
        "COA": read(cfg.sheet_coa),
        "PC": read(cfg.sheet_pc),
        "LOGIC": read(cfg.sheet_logic),
        "ALLOCATION": read(cfg.sheet_allocation),
    }
    logger.info("extracted: " + ", ".join(f"{k}={len(v)}" for k, v in sheets.items()))
    return sheets


# ---------------------------------------------------------------------------
# 2. Build lookups
# ---------------------------------------------------------------------------
def build_lookups(cfg: Config, sheets: Dict[str, pd.DataFrame]) -> Lookups:
    pc = sheets["PC"].copy()
    coa = sheets["COA"].copy()
    logic = sheets["LOGIC"].copy()
    alloc = sheets["ALLOCATION"].copy()

    pc["_dept_key"] = _norm(pc["Dept"])
    dept_to_bucket = dict(zip(pc["_dept_key"], _strip(pc["PC"])))
    dept_to_div = dict(zip(pc["_dept_key"], _strip(pc["Div"])))
    dept_to_pc = dict(zip(pc["_dept_key"], _strip(pc["PC"])))

    code_to_reporting = dict(zip(_strip(coa["Code"]), _strip(coa["Reporting Line"])))

    # LOGIC composite rule key: (Account Code, Account Name, bucket). A single
    # (Account Code, bucket) can carry different methods per account name — e.g.
    # 7201-00-000 under Head Quarter is Revenue HO for "…Car Vehicle" but
    # FTE - Head Office for "…Off. Building" — so Account Name is part of the key.
    logic["_code_key"] = _strip(logic["Account Code"])
    logic["_acct_key"] = _norm(logic["Account Name"])
    logic["_bucket_key"] = _norm(logic["PC"])
    logic["Distribution"] = _strip(logic["Distribution"])
    dupes = logic.duplicated(subset=["_code_key", "_acct_key", "_bucket_key"], keep=False)
    if dupes.any():
        sample = logic.loc[dupes, ["Account Code", "Account Name", "PC", "Distribution"]].head(10)
        raise ValueError(
            f"Ambiguous LOGIC rule keys (Account Code, Account Name, PC bucket) — "
            f"{int(dupes.sum())} rows:\n{sample}"
        )
    rule = logic[["_code_key", "_acct_key", "_bucket_key", "Distribution"]].rename(
        columns={"Distribution": _METHOD}
    )

    # ALLOCATION split into the account-name-conditional branch and the rest.
    alloc["Distribution"] = _strip(alloc["Distribution"])
    alloc["New Dept"] = _strip(alloc["New Dept"])
    alloc["Percentage"] = pd.to_numeric(alloc["Percentage"], errors="raise")
    alloc["_acct_key"] = _norm(alloc["Account Name"])
    is_lab = alloc["Distribution"] == cfg.lab_distribution_method
    alloc_lab = alloc[is_lab].copy()
    alloc_other = alloc[~is_lab].copy()

    return Lookups(
        dept_to_bucket=dept_to_bucket,
        dept_to_div=dept_to_div,
        dept_to_pc=dept_to_pc,
        code_to_reporting=code_to_reporting,
        rule=rule,
        alloc_lab=alloc_lab,
        alloc_other=alloc_other,
        pc_depts=set(pc["_dept_key"].dropna()),
        coa_codes=set(_strip(coa["Code"]).dropna()),
    )


# ---------------------------------------------------------------------------
# 3. Resolve rule (Steps A + B)
# ---------------------------------------------------------------------------
def resolve_rule(gl: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    df = gl.copy().reset_index(drop=True)
    df[_GL_ID] = np.arange(len(df))

    # Normalise / coerce measures.
    df["Debit"] = pd.to_numeric(df["Debit"], errors="raise").fillna(0.0)
    df["Credit"] = pd.to_numeric(df["Credit"], errors="raise").fillna(0.0)
    df["Amount"] = df["Debit"] - df["Credit"]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    df["Dept"] = _strip(df["Dept"])
    df["Account Code"] = _strip(df["Account Code"])
    df["Account Name"] = df["Account Name"].astype("string")

    # Step A — bucket + reporting line.
    dept_key = _norm(df["Dept"])
    df[_BUCKET] = dept_key.map(lk.dept_to_bucket)
    df["Reporting Account Name"] = df["Account Code"].map(lk.code_to_reporting)

    # Step B — resolve method on (Account Code, Account Name, bucket).
    df["_code_key"] = df["Account Code"]
    df["_acct_key"] = _norm(df["Account Name"])
    df["_bucket_key"] = _norm(df[_BUCKET])
    df = df.merge(lk.rule, on=["_code_key", "_acct_key", "_bucket_key"], how="left")

    return df


# ---------------------------------------------------------------------------
# 4. Distribute (Step C) + capture rejects
# ---------------------------------------------------------------------------
def distribute(cfg: Config, resolved: pd.DataFrame, lk: Lookups):
    """Return (allocated_children, rejects). One child row per receiving dept."""
    is_direct = resolved[_METHOD].isna()
    direct = resolved[is_direct].copy()
    to_dist = resolved[~is_direct].copy()

    # -- Direct charges: one row, New Dept = Dept, Percentage = 1 -----------
    direct["New Dept"] = direct["Dept"]
    direct["Percentage"] = 1.0
    direct["Allocation"] = direct["Amount"]

    # -- Distributed -------------------------------------------------------
    is_lab = to_dist[_METHOD] == cfg.lab_distribution_method
    lab_src = to_dist[is_lab].copy()
    other_src = to_dist[~is_lab].copy()

    # Non-lab: join on method only.
    other_alloc = lk.alloc_other[["Distribution", "New Dept", "Percentage"]]
    other_j = other_src.merge(
        other_alloc, left_on=_METHOD, right_on="Distribution", how="left"
    )

    # Lab: join on method AND normalised Account Name.
    lab_src["_acct_key"] = _norm(lab_src["Account Name"])
    lab_alloc = lk.alloc_lab[["Distribution", "_acct_key", "New Dept", "Percentage"]]
    lab_j = lab_src.merge(
        lab_alloc,
        left_on=[_METHOD, "_acct_key"],
        right_on=["Distribution", "_acct_key"],
        how="left",
    )

    distributed = pd.concat([other_j, lab_j], ignore_index=True)

    # A resolved method with no ALLOCATION match => reject (do not drop).
    unmatched = distributed["New Dept"].isna()
    rejects = distributed[unmatched].copy()
    distributed = distributed[~unmatched].copy()
    distributed["Allocation"] = distributed["Amount"] * distributed["Percentage"]

    children = pd.concat([direct, distributed], ignore_index=True)
    return children, rejects


# ---------------------------------------------------------------------------
# 5. Enrich (Step D) + derived columns
# ---------------------------------------------------------------------------
def enrich(children: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    df = children.copy()
    new_dept_key = _norm(df["New Dept"])
    df["Div"] = new_dept_key.map(lk.dept_to_div)
    df["PC"] = new_dept_key.map(lk.dept_to_pc)

    # Derived strings.
    df["Code"] = df["Account Code"]
    df["Account"] = (
        df["Code"].astype("string").fillna("") + " " + df["Account Name"].fillna("")
    ).str.strip()
    if "Note" not in df.columns:
        df["Note"] = pd.NA
    # Audit label: method / receiving dept (blank for direct charges).
    label = df[_METHOD].astype("string").fillna("") + " / " + df["New Dept"].fillna("")
    df["Distribution And Allocation"] = label.where(df[_METHOD].notna(), other=pd.NA)

    # Deterministic ordering: source line, then receiving dept.
    df = df.sort_values([_GL_ID, "New Dept"], kind="stable").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 6. Validate (§5 hard gates)
# ---------------------------------------------------------------------------
@dataclass
class Recon:
    source_total: float
    allocated_total: float
    variance: float
    n_gl_lines: int
    n_output_rows: int
    n_direct: int
    n_distributed: int
    n_rejects: int
    ok: bool


def validate(cfg: Config, resolved: pd.DataFrame, out: pd.DataFrame,
             rejects: pd.DataFrame, lk: Lookups) -> Recon:
    tol = cfg.tolerance
    breaches = []

    source_total = float(resolved["Amount"].sum())
    allocated_total = float(out["Allocation"].sum())
    variance = allocated_total - source_total

    # 1. Grand-total tie-out.
    if abs(variance) > tol:
        breaches.append(f"grand-total variance {variance:.6f} > tol {tol}")

    # 2. Per-line conservation.
    per_line = out.groupby(_GL_ID)["Allocation"].sum()
    src_line = resolved.set_index(_GL_ID)["Amount"]
    diff = (per_line - src_line).abs()
    bad_lines = diff[diff > tol]
    if len(bad_lines):
        breaches.append(f"{len(bad_lines)} GL line(s) fail Σchild==Amount")

    # 3. Percentage integrity (warn only).
    for method, grp in pd.concat([lk.alloc_other, lk.alloc_lab]).groupby(
            ["Distribution", "_acct_key"], dropna=False):
        s = grp["Percentage"].sum()
        if abs(s - 1.0) > 1e-6:
            logger.warning("percentage set != 1 for %s: sum=%.6f", method, s)

    # 4. No orphan rules.
    if len(rejects):
        breaches.append(f"{len(rejects)} rejected row(s) (method with no ALLOCATION match)")

    # 5. Referential integrity (warn — list misses).
    miss_dept = set(_norm(out["New Dept"]).dropna()) - lk.pc_depts
    miss_dept |= set(_norm(resolved["Dept"]).dropna()) - lk.pc_depts
    if miss_dept:
        logger.warning("Dept/New Dept not in PC master: %s", sorted(miss_dept)[:10])
    miss_code = set(resolved["Account Code"].dropna()) - lk.coa_codes
    if miss_code:
        logger.warning("Account Code not in COA: %s", sorted(miss_code)[:10])

    # 6. Row-count sanity.
    n_direct_rows = int(out[_METHOD].isna().sum())
    n_distributed = int(out[_METHOD].notna().sum())
    expected_direct = int(resolved[_METHOD].isna().sum())
    if n_direct_rows != expected_direct:
        breaches.append(
            f"direct row count {n_direct_rows} != GL lines without a rule {expected_direct}"
        )

    # 7. Sign check.
    neg_src = float(resolved.loc[resolved["Amount"] < 0, "Amount"].sum())
    neg_alloc = float(out.loc[out["Allocation"] < 0, "Allocation"].sum())
    logger.info("sign check: negative source=%.2f allocated(neg rows)=%.2f", neg_src, neg_alloc)

    recon = Recon(
        source_total=source_total,
        allocated_total=allocated_total,
        variance=variance,
        n_gl_lines=int(resolved[_GL_ID].nunique()),
        n_output_rows=len(out),
        n_direct=n_direct_rows,
        n_distributed=n_distributed,
        n_rejects=len(rejects),
        ok=not breaches,
    )

    logger.info(
        "RECON source=%.2f allocated=%.2f variance=%.6f | rows=%d direct=%d dist=%d rejects=%d",
        recon.source_total, recon.allocated_total, recon.variance,
        recon.n_output_rows, recon.n_direct, recon.n_distributed, recon.n_rejects,
    )

    if breaches:
        raise ValueError("VALIDATION FAILED:\n  - " + "\n  - ".join(breaches))
    return recon


# ---------------------------------------------------------------------------
# 7. Load
# ---------------------------------------------------------------------------
def _final_frame(out: pd.DataFrame) -> pd.DataFrame:
    df = out.copy()
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[OUTPUT_COLUMNS]


def load(cfg: Config, out: pd.DataFrame, rejects: pd.DataFrame, recon: Recon) -> None:
    import os
    os.makedirs(os.path.dirname(cfg.output_path), exist_ok=True)

    dist = _final_frame(out)
    recon_df = pd.DataFrame([{
        "Source Total": recon.source_total,
        "Allocated Total": recon.allocated_total,
        "Variance": recon.variance,
        "GL Lines": recon.n_gl_lines,
        "Output Rows": recon.n_output_rows,
        "Direct": recon.n_direct,
        "Distributed": recon.n_distributed,
        "Rejects": recon.n_rejects,
    }])

    with pd.ExcelWriter(cfg.output_path, engine="openpyxl") as xw:
        dist.to_excel(xw, sheet_name="Distribution", index=False)
        recon_df.to_excel(xw, sheet_name="Reconciliation", index=False)
        if len(rejects):
            rejects.to_excel(xw, sheet_name="rejects", index=False)
    logger.info("wrote %s (%d rows)", cfg.output_path, len(dist))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run(cfg: Config, dry_run: bool = False):
    sheets = extract(cfg)
    lk = build_lookups(cfg, sheets)
    resolved = resolve_rule(sheets["GL"], lk)
    children, rejects = distribute(cfg, resolved, lk)
    out = enrich(children, lk)
    recon = validate(cfg, resolved, out, rejects, lk)
    if dry_run:
        logger.info("dry-run: validation passed, nothing written")
    else:
        load(cfg, out, rejects, recon)
    return out, rejects, recon


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated Cost Distribution ETL")
    parser.add_argument("--dry-run", action="store_true",
                        help="run validation without writing output")
    parser.add_argument("--input", help="override input workbook path")
    parser.add_argument("--output", help="override output workbook path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config()
    if args.input:
        cfg = Config(input_path=args.input, output_path=cfg.output_path)
    if args.output:
        cfg = Config(input_path=cfg.input_path, output_path=args.output)

    run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
