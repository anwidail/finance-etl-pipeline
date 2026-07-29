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
from dataclasses import dataclass, replace
from typing import Dict

import numpy as np
import pandas as pd

from cost_distribution.config import (
    Config, OUTPUT_COLUMNS, OUTPUT_HEADERS, load_config,
)
from cost_distribution.periods import (
    date_to_period, date_to_period_series, month_name_id, normalize_period,
    period_to_date, previous_period,
)

logger = logging.getLogger("cost_distribution")

# Internal-only helper columns, stripped before writing the final frame.
_BUCKET = "_bucket"
_METHOD = "_method"
_GL_ID = "gl_line_id"
# Marks rows booked on a credit-balance (revenue) account, so the overhead sweep
# can leave them alone: it redistributes cost, not what a department earned.
_CREDIT_SIDE = "_credit_side"


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
    dept_to_code: Dict[str, str]            # norm(Dept) -> Dept Code
    code_to_bucket: Dict[str, str]          # Dept Code -> PC bucket
    code_to_div: Dict[str, str]             # Dept Code -> Div
    code_to_pc: Dict[str, str]              # Dept Code -> PC
    code_to_dept: Dict[str, str]            # Dept Code -> Dept (canonical name)
    code_to_reporting: Dict[str, str]       # Account Code -> Reporting Line
    code_to_reporting_code: Dict[str, str]  # Account Code -> Reporting Code
    code_to_reporting_acct: Dict[str, str]  # Account Code -> Reporting Account
    rule: pd.DataFrame                      # LOGIC with norm keys
    alloc_lab: pd.DataFrame                 # ALLOCATION rows for Lab Distribution
    alloc_other: pd.DataFrame               # ALLOCATION rows for all other methods
    pc_depts: set                           # norm(Dept) present in PC
    coa_codes: set                          # Account Code present in COA
    sweep_depts: set                        # norm(Dept) in the overhead sweep bucket


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
        "FTE": read(cfg.sheet_fte),
        "REV": read(cfg.sheet_rev),
    }
    # RL (the reporting-line master) is a V.05 addition and is reference-only —
    # COA already carries the resolved code/line per account. Absent in older
    # workbooks, so its absence is tolerated.
    if cfg.sheet_rl in xl.sheet_names:
        sheets["RL"] = read(cfg.sheet_rl)
    logger.info("extracted: " + ", ".join(f"{k}={len(v)}" for k, v in sheets.items()))
    return sheets


def filter_gl_to_period(gl: pd.DataFrame, period: str) -> pd.DataFrame:
    """Keep only GL lines whose Date falls in ``period`` (MMM-YYYY, e.g. APR-2026).

    This ties the monthly basis to the GL of the same month: a period's
    ALLOCATION/FTE/REV factors are applied only to that period's cost lines.
    Raises if no line matches; warns on lines dropped from other periods or with
    an unparseable date.
    """
    dates = pd.to_datetime(gl["Date"], errors="coerce")
    gl_period = date_to_period_series(gl["Date"])
    mask = gl_period == period

    n_total = len(gl)
    n_match = int(mask.sum())
    n_bad = int(dates.isna().sum())
    if n_match == 0:
        raise ValueError(
            f"No GL lines fall in period {period} (of {n_total} lines). "
            f"Check --period against the workbook's GL dates."
        )
    if n_match < n_total:
        logger.warning(
            "GL filtered to period %s: kept %d of %d lines (dropped %d other-period, %d undated)",
            period, n_match, n_total, n_total - n_match - n_bad, n_bad,
        )
    else:
        logger.info("GL all in period %s (%d lines)", period, n_match)
    return gl[mask].reset_index(drop=True)


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
    dept_to_code = (dict(zip(pc["_dept_key"], _strip(pc["Dept Code"])))
                    if "Dept Code" in pc.columns else {})

    # The same three lookups keyed on the department *code*. The code is what
    # survives a rename — "Automotive (…Technical Ops)" became "Automotive" and
    # "CERTIFICATION SERVICES" became "System Certification Services", leaving
    # ledger rows whose stored name no longer appears in the PC master at all.
    if "Dept Code" in pc.columns:
        code_key = _strip(pc["Dept Code"])
        code_to_bucket = dict(zip(code_key, _strip(pc["PC"])))
        code_to_div = dict(zip(code_key, _strip(pc["Div"])))
        code_to_pc = dict(zip(code_key, _strip(pc["PC"])))
        code_to_dept = dict(zip(code_key, _strip(pc["Dept"])))
    else:
        code_to_bucket = code_to_div = code_to_pc = code_to_dept = {}

    coa_code = _strip(coa["Code"])
    code_to_reporting = dict(zip(coa_code, _strip(coa["Reporting Line"])))
    # V.05 COA also carries the reporting *code* and the concatenated
    # "<code> <line>" label. Older workbooks have neither: fall back to an empty
    # code and derive the label from the reporting line alone.
    if "Reporting Code" in coa.columns:
        rep_code = _strip(coa["Reporting Code"])
    else:
        rep_code = pd.Series(pd.NA, index=coa.index, dtype="string")
    if "Reporting Account" in coa.columns:
        rep_acct = _strip(coa["Reporting Account"])
    else:
        rep_acct = (rep_code.fillna("") + " " + _strip(coa["Reporting Line"]).fillna("")).str.strip()
    code_to_reporting_code = dict(zip(coa_code, rep_code))
    code_to_reporting_acct = dict(zip(coa_code, rep_acct))

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
        dept_to_code=dept_to_code,
        code_to_bucket=code_to_bucket,
        code_to_div=code_to_div,
        code_to_pc=code_to_pc,
        code_to_dept=code_to_dept,
        code_to_reporting=code_to_reporting,
        code_to_reporting_code=code_to_reporting_code,
        code_to_reporting_acct=code_to_reporting_acct,
        rule=rule,
        alloc_lab=alloc_lab,
        alloc_other=alloc_other,
        pc_depts=set(pc["_dept_key"].dropna()),
        coa_codes=set(coa_code.dropna()),
        sweep_depts=set(pc.loc[_norm(pc["PC"]) == _norm(
            pd.Series([cfg.overhead_sweep_pc])).iloc[0], "_dept_key"].dropna()),
    )


# ---------------------------------------------------------------------------
# 3. Resolve rule (Steps A + B)
# ---------------------------------------------------------------------------
def is_credit_balance(cfg: Config, account_code: pd.Series) -> pd.Series:
    """True for accounts that naturally carry a credit balance (revenue)."""
    if not cfg.credit_balance_prefixes:
        return pd.Series(False, index=account_code.index)
    return account_code.astype("string").str.startswith(
        tuple(cfg.credit_balance_prefixes), na=False)


def resolve_rule(cfg: Config, gl: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    df = gl.copy().reset_index(drop=True)
    df[_GL_ID] = np.arange(len(df))

    # Normalise / coerce measures.
    df["Debit"] = pd.to_numeric(df["Debit"], errors="raise").fillna(0.0)
    df["Credit"] = pd.to_numeric(df["Credit"], errors="raise").fillna(0.0)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    df["Dept"] = _strip(df["Dept"])
    df["Account Code"] = _strip(df["Account Code"])
    # Cost is Debit − Credit; revenue is the mirror, so both read positive.
    df[_CREDIT_SIDE] = is_credit_balance(cfg, df["Account Code"])
    df["Amount"] = np.where(df[_CREDIT_SIDE],
                            df["Credit"] - df["Debit"],
                            df["Debit"] - df["Credit"])
    df["Account Name"] = df["Account Name"].astype("string")

    # Step A — bucket + reporting line/code (all three from COA, keyed on the
    # account code, per the INFO sheet's column sources).
    #
    # The bucket is resolved from the department *code* where the feed supplies
    # one, and from the name only as a fallback. Codes outlive renames; names do
    # not. Feeds without a code (sales_detail) still resolve by name, so the
    # fallback is load-bearing, not defensive.
    dept_key = _norm(df["Dept"])
    by_name = dept_key.map(lk.dept_to_bucket)
    if "Dept Code" in df.columns and lk.code_to_bucket:
        df["Dept Code"] = _strip(df["Dept Code"])
        by_code = df["Dept Code"].map(lk.code_to_bucket)
        df[_BUCKET] = by_code.fillna(by_name)
        disagreed = int((by_code.notna() & by_name.notna() & (by_code != by_name)).sum())
        if disagreed:
            logger.info("bucket taken from the department code on %d line(s) where "
                        "the stored name says otherwise (renamed departments)",
                        disagreed)
        rescued = int((by_code.notna() & by_name.isna()).sum())
        if rescued:
            logger.info("department code resolved %d line(s) whose name is not in "
                        "the PC master", rescued)
    else:
        df[_BUCKET] = by_name
    df["Reporting Account Name"] = df["Account Code"].map(lk.code_to_reporting)
    df["Reporting Code"] = df["Account Code"].map(lk.code_to_reporting_code)
    df["Reporting Account"] = df["Account Code"].map(lk.code_to_reporting_acct)

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
    if "Dept Code" not in df.columns:
        df["Dept Code"] = pd.NA

    # Resolve the receiving department's code, then read Div/PC off *that*.
    #
    # Going by name here is what put cost in the wrong profit centre: a direct
    # charge takes its New Dept from the GL line's department name, and Zahir's
    # callbacks still emit D010 under its former name — which happens to be the
    # current name of a different department, D01. The name lookup then landed
    # those lines in D01's profit centre.
    #
    # A direct charge keeps the originating code exactly; a distributed row is
    # named by ALLOCATION, so its code is resolved from that name.
    new_dept_key = _norm(df["New Dept"])
    by_name = new_dept_key.map(lk.dept_to_code)
    is_direct = df[_METHOD].isna()
    df["New Dept Code"] = _strip(df["Dept Code"]).where(is_direct, by_name)
    if lk.dept_to_code:
        df["New Dept Code"] = df["New Dept Code"].fillna(by_name)

    code_key = _strip(df["New Dept Code"])
    df["Div"] = code_key.map(lk.code_to_div).fillna(new_dept_key.map(lk.dept_to_div))
    df["PC"] = code_key.map(lk.code_to_pc).fillna(new_dept_key.map(lk.dept_to_pc))
    # Show the department the code actually names, so the row is self-consistent.
    canonical = code_key.map(lk.code_to_dept)
    renamed = int((canonical.notna() & (canonical != _strip(df["New Dept"]))).sum())
    if renamed:
        logger.info("receiving department renamed to match its code on %d row(s)", renamed)
    df["New Dept"] = canonical.fillna(_strip(df["New Dept"]))

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

    # Period derived per-row from the GL date (MMM-YYYY), same rule as the GL
    # filter — so the distribution snapshot is self-describing by month.
    df["period"] = date_to_period_series(df["Date"])

    # Deterministic ordering: source line, then receiving dept.
    df = df.sort_values([_GL_ID, "New Dept"], kind="stable").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 6. Overhead sweep (Step E — new in V.05)
# ---------------------------------------------------------------------------
def overhead_weights(cfg: Config, lk: Lookups) -> pd.Series:
    """Receiving dept -> share of the overhead pool (index keeps roster order).

    The roster is ``overhead_basis_method``'s ALLOCATION rows minus the swept
    (administrative) depts, renormalised over what is left and rounded to the
    workbook's stated precision. Rounding can leave the shares off 1.0 by a few
    ulps of the last decimal; the residual is pushed onto the largest receiver so
    the sweep stays exactly zero-sum.
    """
    basis = lk.alloc_other[lk.alloc_other["Distribution"] == cfg.overhead_basis_method]
    if basis.empty:
        raise ValueError(
            f"Overhead sweep needs ALLOCATION method {cfg.overhead_basis_method!r}, "
            f"which has no rows. Disable it with --no-overhead or fix the basis."
        )
    keep = basis[~_norm(basis["New Dept"]).isin(lk.sweep_depts)]
    share = keep.groupby("New Dept", sort=False)["Percentage"].sum()
    total = float(share.sum())
    if total <= 0:
        raise ValueError(
            f"Overhead basis {cfg.overhead_basis_method!r} has no weight left after "
            f"excluding the {cfg.overhead_sweep_pc!r} depts."
        )
    weights = (share / total).round(cfg.overhead_pct_decimals)
    residual = 1.0 - float(weights.sum())
    # Ignore pure float noise; correct only a real rounding gap.
    if abs(residual) > 1e-12:
        top = weights.idxmax()
        weights.loc[top] += residual
        logger.warning("overhead weights sum to %.10f after rounding to %d dp; "
                       "residual %+.10f placed on %s",
                       1.0 - residual, cfg.overhead_pct_decimals, residual, top)
    return weights


def sweep_overhead(cfg: Config, out: pd.DataFrame, lk: Lookups) -> pd.DataFrame:
    """Return the overhead reallocation rows appended after the normal distribution.

    Per period in ``out``: every department in the ``overhead_sweep_pc`` bucket
    has its whole allocated cost reversed (one row at 100%), and the resulting
    pool is charged to the revenue-generating depts by ``overhead_weights``. The
    block sums to zero, so the grand total is untouched.
    """
    if not cfg.overhead_enabled or out.empty:
        return out.iloc[0:0].copy()

    weights = overhead_weights(cfg, lk)
    blocks = []

    for period, grp in out.groupby("period", dropna=True, sort=True):
        # Revenue earned by an administrative department settles against its own
        # cost first; only the net is pushed out to the other centres. Both sides
        # are stored positive, so revenue enters the pool with its sign flipped.
        net = np.where(is_credit_balance(cfg, grp["Code"]),
                       -grp["Allocation"], grp["Allocation"])
        swept = grp.assign(_net=net)[_norm(grp["New Dept"]).isin(lk.sweep_depts)]
        pool_by_dept = swept.groupby("New Dept", sort=False)["_net"].sum()
        pool_by_dept = pool_by_dept[pool_by_dept != 0]
        pool = float(pool_by_dept.sum())
        if not len(pool_by_dept):
            logger.info("overhead sweep %s: no %s cost to sweep", period, cfg.overhead_sweep_pc)
            continue

        prev = previous_period(period)
        label = f"FTE : PC {month_name_id(prev)}"
        stamp = period_to_date(period)
        description = f"{cfg.overhead_account_name} {stamp:%m%y}"

        # Reversal: one row per swept dept, at 100% of its accumulated cost.
        rows = [{"New Dept": dept, "Amount": -amount, "Percentage": 1.0,
                 "Allocation": -amount}
                for dept, amount in pool_by_dept.items()]
        # Charge-out: the pool spread over the receiving depts.
        rows += [{"New Dept": dept, "Amount": pool, "Percentage": float(weight),
                  "Allocation": pool * float(weight)}
                 for dept, weight in weights.items()]

        block = pd.DataFrame(rows)
        block["Date"] = stamp
        block["period"] = period
        block["Dept"] = block["New Dept"]            # booked on the dept itself
        block["Description"] = description
        block["Code"] = cfg.overhead_account_code
        block["Account Name"] = cfg.overhead_account_name
        block["Distribution And Allocation"] = label
        block[_METHOD] = label
        blocks.append(block)

    if not blocks:
        return out.iloc[0:0].copy()

    df = pd.concat(blocks, ignore_index=True)
    df["Account"] = (df["Code"] + " " + df["Account Name"]).str.strip()
    df["Reporting Account Name"] = df["Code"].map(lk.code_to_reporting)
    df["Reporting Code"] = df["Code"].map(lk.code_to_reporting_code)
    df["Reporting Account"] = df["Code"].map(lk.code_to_reporting_acct)
    # These rows are generated by the distribution, not read off a GL line, so
    # there is no code to inherit. The department name is one this engine chose
    # from the PC master, so the master resolves it straight back to its code —
    # and Div/PC then come from the code like everywhere else.
    dept_key = _norm(df["New Dept"])
    code = dept_key.map(lk.dept_to_code)
    df["Dept Code"] = code
    df["New Dept Code"] = code
    df["Div"] = code.map(lk.code_to_div).fillna(dept_key.map(lk.dept_to_div))
    df["PC"] = code.map(lk.code_to_pc).fillna(dept_key.map(lk.dept_to_pc))
    unresolved = int(code.isna().sum())
    if unresolved:
        logger.warning("overhead sweep: %d row(s) whose department is not in the "
                       "PC master, left without a code", unresolved)
    # Debit/Credit restate the signed Amount the way the GL feed does.
    df["Debit"] = df["Amount"].clip(lower=0.0)
    df["Credit"] = (-df["Amount"]).clip(lower=0.0)
    # No source journal line: these rows are derived, not traced to a GL line.
    df[_GL_ID] = pd.NA

    logger.info("overhead sweep: %d rows over %d period(s), net %.6f",
                len(df), len(blocks), float(df["Allocation"].sum()))
    return df


# ---------------------------------------------------------------------------
# 7. Validate (§5 hard gates)
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
    n_overhead: int
    overhead_pool: float
    ok: bool


def validate(cfg: Config, resolved: pd.DataFrame, out: pd.DataFrame,
             rejects: pd.DataFrame, lk: Lookups,
             overhead: pd.DataFrame = None) -> Recon:
    tol = cfg.tolerance
    breaches = []
    if overhead is None:
        overhead = out.iloc[0:0]

    source_total = float(resolved["Amount"].sum())
    overhead_net = float(overhead["Allocation"].sum()) if len(overhead) else 0.0
    allocated_total = float(out["Allocation"].sum()) + overhead_net
    variance = allocated_total - source_total

    # 1. Grand-total tie-out. Float64 summation noise grows with the size of the
    # total, so the gate is absolute-or-relative — still far tighter than a cent.
    total_tol = max(tol, abs(source_total) * cfg.rel_tolerance)
    if abs(variance) > total_tol:
        breaches.append(f"grand-total variance {variance:.6f} > tol {total_tol:.9f}")

    # 1b. The overhead sweep only moves cost between departments — it must never
    # change the total, or it is silently creating/destroying cost.
    if len(overhead) and abs(overhead_net) > total_tol:
        breaches.append(f"overhead sweep is not zero-sum: net {overhead_net:.6f}")

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

    # Overhead pool = the cost swept out of the administrative depts (the
    # reversal rows are the negative half of a zero-sum block).
    pool = float(-overhead.loc[overhead["Allocation"] < 0, "Allocation"].sum()) if len(overhead) else 0.0

    recon = Recon(
        source_total=source_total,
        allocated_total=allocated_total,
        variance=variance,
        n_gl_lines=int(resolved[_GL_ID].nunique()),
        n_output_rows=len(out) + len(overhead),
        n_direct=n_direct_rows,
        n_distributed=n_distributed,
        n_rejects=len(rejects),
        n_overhead=len(overhead),
        overhead_pool=pool,
        ok=not breaches,
    )

    logger.info(
        "RECON source=%.2f allocated=%.2f variance=%.6f | rows=%d direct=%d dist=%d "
        "overhead=%d (pool=%.2f) rejects=%d",
        recon.source_total, recon.allocated_total, recon.variance,
        recon.n_output_rows, recon.n_direct, recon.n_distributed,
        recon.n_overhead, recon.overhead_pool, recon.n_rejects,
    )

    if breaches:
        raise ValueError("VALIDATION FAILED:\n  - " + "\n  - ".join(breaches))
    return recon


# ---------------------------------------------------------------------------
# 7. Load
# ---------------------------------------------------------------------------
def _final_frame(out: pd.DataFrame) -> pd.DataFrame:
    """Project onto the output contract and take the workbook's own headers."""
    df = out.copy()
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[OUTPUT_COLUMNS].rename(columns=OUTPUT_HEADERS)


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
        "Overhead": recon.n_overhead,
        "Overhead Pool": recon.overhead_pool,
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
def run(cfg: Config, dry_run: bool = False, recompute_basis: bool = False,
        to_db: bool = False, period: str = None, basis_from_db: bool = False,
        gl_from_db: bool = False, overhead: bool = None):
    if overhead is not None and overhead != cfg.overhead_enabled:
        cfg = replace(cfg, overhead_enabled=overhead)
    sheets = extract(cfg)

    # Optionally take the GL fact from MySQL for this period instead of the
    # workbook (a fully DB-driven monthly run).
    if gl_from_db:
        if not period:
            raise ValueError("--gl-from-db requires --period MMM-YYYY")
        from load.cost_distribution_gl import read_gl_from_db
        from load.cost_distribution_db import get_cost_engine
        sheets["GL"] = read_gl_from_db(period, get_cost_engine())
        logger.info("loaded GL from cost_distribution_db for period %s (%d lines)",
                    period, len(sheets["GL"]))

    # Tie the run to a month: apply this period's basis only to this period's GL.
    if period:
        sheets["GL"] = filter_gl_to_period(sheets["GL"], period)

    # Optionally take the reference/basis sheets from the editable MySQL tables
    # for this period instead of the workbook. GL (the monthly fact) still comes
    # from the workbook feed.
    if basis_from_db:
        if not period:
            raise ValueError("--basis-from-db requires --period YYYY-MM")
        from load.cost_distribution_basis import read_basis_from_db
        from load.cost_distribution_db import get_cost_engine
        db_sheets = read_basis_from_db(period, get_cost_engine())
        sheets.update(db_sheets)  # PC/COA/LOGIC/ALLOCATION/FTE/REV from DB
        logger.info("loaded basis from cost_distribution_db for period %s", period)

    refreshed = None
    if recompute_basis:
        from cost_distribution.basis import recompute_allocation, verify_against
        refreshed = recompute_allocation(cfg, sheets["ALLOCATION"], sheets["FTE"], sheets["REV"])
        verify_against(cfg, refreshed, sheets["ALLOCATION"])
        sheets["ALLOCATION"] = refreshed
    lk = build_lookups(cfg, sheets)
    resolved = resolve_rule(cfg, sheets["GL"], lk)
    children, rejects = distribute(cfg, resolved, lk)
    base = enrich(children, lk)
    # Step E — sweep administrative overhead onto the revenue-generating depts.
    # Validated against the *base* output so the per-line conservation and
    # direct-row gates still see one row set per GL line.
    overhead = sweep_overhead(cfg, base, lk)
    recon = validate(cfg, resolved, base, rejects, lk, overhead)
    out = pd.concat([base, overhead], ignore_index=True) if len(overhead) else base
    if dry_run:
        logger.info("dry-run: validation passed, nothing written")
        return out, rejects, recon

    # Any DB write for this period (snapshot or recompute-persist) requires the
    # period to be open — check before writing anything so a closed period stays
    # fully frozen.
    persist = refreshed is not None and period
    writes_db = to_db or persist
    engine = None
    if writes_db:
        from load.cost_distribution_db import get_cost_engine
        from load.cost_distribution_period import assert_period_open
        engine = get_cost_engine()
        assert_period_open(engine, period, "write cost_distribution_db")

    load(cfg, out, rejects, recon)

    # Persist the recomputed ALLOCATION back so the stored basis stays in sync
    # (full replace of this period's rows — no duplicate build-up).
    if persist:
        from load.cost_distribution_basis import persist_allocation
        n = persist_allocation(period, refreshed, engine)
        logger.info("persisted recomputed ALLOCATION to basis_allocation "
                    "for period %s (%d rows)", period, n)

    if to_db:
        from load.cost_distribution_db import load_to_db
        run_id = load_to_db(out, recon, cfg, recompute_basis=recompute_basis,
                            period=period, engine=engine)
        logger.info("loaded snapshot to cost_distribution_db (run_id=%s, period=%s)",
                    run_id, period)
    return out, rejects, recon


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated Cost Distribution ETL")
    parser.add_argument("--dry-run", action="store_true",
                        help="run validation without writing output")
    parser.add_argument("--recompute-basis", action="store_true",
                        help="recompute FTE-*/Revenue-* factors from the FTE/REV sheets")
    parser.add_argument("--to-db", action="store_true",
                        help="also load the snapshot into cost_distribution_db (MySQL)")
    parser.add_argument("--period", help="monthly basis period, MMM-YYYY e.g. APR-2026")
    parser.add_argument("--basis-from-db", action="store_true",
                        help="read PC/COA/LOGIC/ALLOCATION/FTE/REV from the DB basis "
                             "tables for --period instead of the workbook")
    parser.add_argument("--import-basis", action="store_true",
                        help="seed the DB basis tables for --period from the workbook, then exit")
    parser.add_argument("--reseed-global", action="store_true",
                        help="with --import-basis, also refresh the global policy tables "
                             "(PC/COA/LOGIC) from the workbook")
    parser.add_argument("--verify-basis", action="store_true",
                        help="report whether basis_allocation still agrees with "
                             "basis_fte/basis_rev for --period (read-only), then exit")
    parser.add_argument("--rebuild-allocation", action="store_true",
                        help="refill basis_allocation for --period from basis_fte/basis_rev "
                             "(fixed methods pass through unchanged), then exit")
    parser.add_argument("--import-gl", action="store_true",
                        help="seed the gl_entry table for --period from the workbook, then exit")
    parser.add_argument("--import-gl-from-accounting", action="store_true",
                        help="seed gl_entry for --period from accounting.gl (cost accounts "
                             "only) instead of the workbook, then exit")
    parser.add_argument("--gl-from-db", action="store_true",
                        help="read the GL fact from gl_entry for --period instead of the workbook")
    parser.add_argument("--no-overhead", dest="overhead", action="store_false", default=None,
                        help="skip the overhead sweep (leave admin cost on the admin depts)")
    parser.add_argument("--close-period", action="store_true",
                        help="lock --period against further writes, then exit")
    parser.add_argument("--reopen-period", action="store_true",
                        help="unlock --period, then exit")
    parser.add_argument("--note", help="note stored with --close-period")
    parser.add_argument("--input", help="override input workbook path")
    parser.add_argument("--output", help="override output workbook path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Load .env so COST_DB_* (and any path overrides) are available for --to-db.
    try:
        from dotenv import load_dotenv
        import os as _os
        load_dotenv(_os.path.join(_os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass

    cfg = load_config()
    if args.input:
        cfg = Config(input_path=args.input, output_path=cfg.output_path)
    if args.output:
        cfg = Config(input_path=cfg.input_path, output_path=args.output)

    # Canonicalise the period to MMM-YYYY (accepts APR-2026 or 2026-04).
    period = normalize_period(args.period) if args.period else None

    if args.close_period or args.reopen_period:
        if not period:
            parser.error("--close-period/--reopen-period require --period MMM-YYYY")
        from load.cost_distribution_db import get_cost_engine
        from load.cost_distribution_period import close_period, reopen_period
        engine = get_cost_engine()
        if args.close_period:
            close_period(engine, period, note=args.note)
            logger.info("closed period %s (locked against writes)", period)
        else:
            existed = reopen_period(engine, period)
            logger.info("reopened period %s%s", period,
                        "" if existed else " (was not closed)")
        return

    if args.import_basis:
        if not period:
            parser.error("--import-basis requires --period MMM-YYYY (e.g. APR-2026)")
        from load.cost_distribution_basis import import_basis_from_workbook
        from load.cost_distribution_db import get_cost_engine
        from load.cost_distribution_period import assert_period_open
        engine = get_cost_engine()
        assert_period_open(engine, period, "seed basis")
        counts = import_basis_from_workbook(cfg, period, engine,
                                            refresh_global=args.reseed_global)
        logger.info("seeded basis for period %s: %s (global refreshed=%s)",
                    period, counts, args.reseed_global)
        return

    if args.verify_basis:
        if not period:
            parser.error("--verify-basis requires --period MMM-YYYY (e.g. JUL-2026)")
        from cost_distribution.basis import recompute_allocation, drift_by_method
        from load.cost_distribution_basis import read_basis_from_db
        from load.cost_distribution_db import get_cost_engine
        db = read_basis_from_db(period, get_cost_engine())
        refreshed = recompute_allocation(cfg, db["ALLOCATION"], db["FTE"], db["REV"])
        report = drift_by_method(cfg, refreshed, db["ALLOCATION"])
        logger.info("basis check for %s (%d FTE rows, %d REV rows)",
                    period, len(db["FTE"]), len(db["REV"]))
        for _, r in report.iterrows():
            logger.info("  %-8s %-42s depts=%-3d sum=%.6f drift=%.9f %s",
                        r["basis"], r["method"], r["depts"], r["sum_stored"],
                        r["max_drift"], "OK" if r["in_sync"] else "OUT OF SYNC")
        stale = report[~report["in_sync"]]
        if len(stale):
            logger.warning("%d method(s) out of sync with their basis: %s. "
                           "Refresh with --rebuild-allocation --period %s",
                           len(stale), ", ".join(stale["method"]), period)
            raise SystemExit(1)
        logger.info("all %d method(s) agree with their basis", len(report))
        return

    if args.rebuild_allocation:
        if not period:
            parser.error("--rebuild-allocation requires --period MMM-YYYY (e.g. JUL-2026)")
        from cost_distribution.basis import recompute_allocation, verify_against
        from load.cost_distribution_basis import read_basis_from_db, persist_allocation
        from load.cost_distribution_db import get_cost_engine
        from load.cost_distribution_period import assert_period_open
        engine = get_cost_engine()
        assert_period_open(engine, period, "rebuild basis_allocation")
        db = read_basis_from_db(period, engine)
        # The stored ALLOCATION supplies the receiving-dept roster (depts with a
        # 0% share cannot be discovered from FTE/REV alone) and the fixed splits.
        refreshed = recompute_allocation(cfg, db["ALLOCATION"], db["FTE"], db["REV"])
        drift = verify_against(cfg, refreshed, db["ALLOCATION"])
        n = persist_allocation(period, refreshed, engine)
        logger.info("rebuilt basis_allocation for %s: %d rows written "
                    "(max drift vs previously stored %.9f)", period, n, drift)
        return

    if args.import_gl_from_accounting:
        if not period:
            parser.error("--import-gl-from-accounting requires --period MMM-YYYY")
        from load.cost_distribution_gl import import_gl_from_accounting
        from load.cost_distribution_db import get_cost_engine, get_finance_engine
        from load.cost_distribution_period import assert_period_open
        engine = get_cost_engine()
        assert_period_open(engine, period, "seed gl_entry from accounting")
        rep = import_gl_from_accounting(cfg, period, engine, get_finance_engine())
        logger.info("gl_entry seeded from accounting.gl for %s: %d of %d rows "
                    "(cost %.2f, revenue %.2f; canonicalised %d dept name(s), "
                    "%d account name(s))",
                    rep["period"], rep["imported"], rep["source_rows"],
                    rep["cost"], rep["revenue"], rep["dept_renamed"], rep["account_renamed"])
        for reason, (n, amt) in rep["excluded"].items():
            if n:
                logger.info("  excluded %-22s %5d rows  %18.2f", reason, n, amt)
        return

    if args.import_gl:
        if not period:
            parser.error("--import-gl requires --period MMM-YYYY (e.g. APR-2026)")
        from load.cost_distribution_gl import import_gl_from_workbook
        from load.cost_distribution_db import get_cost_engine
        from load.cost_distribution_period import assert_period_open
        engine = get_cost_engine()
        assert_period_open(engine, period, "seed gl_entry")
        n = import_gl_from_workbook(cfg, period, engine)
        logger.info("seeded gl_entry for period %s: %d lines", period, n)
        return

    run(cfg, dry_run=args.dry_run, recompute_basis=args.recompute_basis,
        to_db=args.to_db, period=period, basis_from_db=args.basis_from_db,
        gl_from_db=args.gl_from_db, overhead=args.overhead)


if __name__ == "__main__":
    main()
