r"""Optional basis recompute — refresh the ALLOCATION split factors from source.

The `ALLOCATION` sheet is authoritative for a normal run, but its `FTE - *` and
`Revenue - *` percentages are *derived*:

- **FTE - \*** — headcount (`HC`) share by `Dept`, within a `Location` scope
  (``FTE - All`` = every employee; the others filter on a Location token). See
  ``Config.fte_scopes``.
- **Revenue - \*** — the matching percentage column on the `REV` sheet, grouped
  by `Div` (which is the receiving `New Dept`). See ``Config.rev_method_column``.

Recomputing keeps the *roster* of receiving departments from the current
`ALLOCATION` (some appear with a 0% share and cannot be derived from `FTE`/`REV`
alone), and only refreshes the percentage values. Use ``recompute_allocation``
to produce a refreshed ALLOCATION frame, and ``verify_against`` to prove the
recomputed factors reproduce the authoritative ones to tolerance.
"""
from __future__ import annotations

import logging
from typing import Dict

import pandas as pd

from cost_distribution.config import Config

logger = logging.getLogger("cost_distribution.basis")


def _strip(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()


def recompute_fte_factors(cfg: Config, fte: pd.DataFrame, roster: Dict[str, list]) -> pd.DataFrame:
    """Return long-form (Distribution, New Dept, Percentage) for every FTE-* method.

    `roster[method]` is the ordered list of receiving departments to emit (taken
    from the current ALLOCATION), so depts with no headcount in scope still
    appear at 0%.
    """
    f = fte.copy()
    f["Dept"] = _strip(f["Dept"])
    f["Location"] = _strip(f["Location"])
    f["HC"] = pd.to_numeric(f["HC"], errors="raise")

    rows = []
    for method, token in cfg.fte_scopes.items():
        scope = f if token is None else f[f["Location"].str.contains(token, case=False, na=False)]
        share = scope.groupby("Dept")["HC"].sum()
        total = share.sum()
        share = share / total if total else share
        for dept in roster.get(method, list(share.index)):
            rows.append({
                "Distribution": method,
                "New Dept": dept,
                "Percentage": float(share.get(dept, 0.0)),
            })
    return pd.DataFrame(rows)


def recompute_revenue_factors(cfg: Config, rev: pd.DataFrame, roster: Dict[str, list]) -> pd.DataFrame:
    """Return long-form (Distribution, New Dept, Percentage) for every Revenue-* method."""
    r = rev.copy()
    r["Div"] = _strip(r["Div"])

    rows = []
    for method, col in cfg.rev_method_column.items():
        share = r.groupby("Div")[col].sum()
        for dept in roster.get(method, list(share.index)):
            rows.append({
                "Distribution": method,
                "New Dept": dept,
                "Percentage": float(share.get(dept, 0.0)),
            })
    return pd.DataFrame(rows)


def _roster_from(alloc: pd.DataFrame, methods) -> Dict[str, list]:
    a = alloc.copy()
    a["Distribution"] = _strip(a["Distribution"])
    a["New Dept"] = _strip(a["New Dept"])
    out = {}
    for m in methods:
        out[m] = list(a.loc[a["Distribution"] == m, "New Dept"])
    return out


def recompute_allocation(cfg: Config, alloc: pd.DataFrame, fte: pd.DataFrame,
                         rev: pd.DataFrame) -> pd.DataFrame:
    """Return a refreshed ALLOCATION: FTE-*/Revenue-* recomputed, rest unchanged.

    Lab Distribution and the remaining fixed-percentage methods pass through
    untouched; only the headcount- and revenue-driven factors are recomputed.
    """
    a = alloc.copy()
    a["Distribution"] = _strip(a["Distribution"])
    a["New Dept"] = _strip(a["New Dept"])

    fte_methods = list(cfg.fte_scopes)
    rev_methods = list(cfg.rev_method_column)
    recomputed_methods = set(fte_methods) | set(rev_methods)

    fte_roster = _roster_from(a, fte_methods)
    rev_roster = _roster_from(a, rev_methods)

    fte_long = recompute_fte_factors(cfg, fte, fte_roster)
    rev_long = recompute_revenue_factors(cfg, rev, rev_roster)

    passthrough = a[~a["Distribution"].isin(recomputed_methods)].copy()
    refreshed = pd.concat([passthrough, fte_long, rev_long], ignore_index=True)
    logger.info("recomputed basis for %d methods (%d rows)",
                len(recomputed_methods), len(fte_long) + len(rev_long))
    return refreshed


def verify_against(cfg: Config, refreshed: pd.DataFrame, authoritative: pd.DataFrame) -> float:
    """Max abs percentage diff between recomputed and authoritative ALLOCATION.

    Returns the max absolute difference (0.0 = exact match). Logs any breaches.
    """
    def key(df):
        d = df.copy()
        d["Distribution"] = _strip(d["Distribution"])
        d["New Dept"] = _strip(d["New Dept"])
        return d.groupby(["Distribution", "New Dept"])["Percentage"].sum()

    ref, auth = key(refreshed), key(authoritative)
    joined = pd.concat([auth.rename("auth"), ref.rename("ref")], axis=1).fillna(0.0)
    diff = (joined["auth"] - joined["ref"]).abs()
    worst = float(diff.max()) if len(diff) else 0.0
    breaches = diff[diff > cfg.tolerance]
    if len(breaches):
        logger.warning("basis recompute differs on %d keys (max %.6f)", len(breaches), worst)
    else:
        logger.info("basis recompute verified: max abs diff %.9f", worst)
    return worst
