"""Tests for the Automated Cost Distribution engine — V.05 behaviour.

Most tests build tiny in-memory sheets so the arithmetic is checkable by hand.
The last one reconciles against the real workbook when it is present.
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from cost_distribution import pipeline as P
from dataclasses import replace

from cost_distribution.config import Config, OUTPUT_COLUMNS, load_config
from cost_distribution.periods import month_name_id, previous_period

# Two administrative depts (swept) and two revenue depts (receiving).
_ADMIN, _REV = "Administration Indonesia", "Auditing Indonesia"


@pytest.fixture
def cfg():
    return Config(input_path="unused.xlsx")


@pytest.fixture
def sheets():
    """Minimal PC/COA/LOGIC/ALLOCATION/GL covering one direct + one swept line."""
    pc = pd.DataFrame([
        {"Dept Code": "A1", "Dept": "Admin One", "Div": "Admin One", "PC": _ADMIN},
        {"Dept Code": "A2", "Dept": "Admin Two", "Div": "Admin Two", "PC": _ADMIN},
        {"Dept Code": "R1", "Dept": "Rev One", "Div": "Rev One", "PC": _REV},
        {"Dept Code": "R2", "Dept": "Rev Two", "Div": "Rev Two", "PC": _REV},
    ])
    coa = pd.DataFrame([
        {"Code": "6101-00-000", "Account Name": "Salary Expense",
         "Reporting Code": "200", "Reporting Line": "Wages and salaries",
         "Reporting Account": "200 Wages and salaries"},
        {"Code": "7501-00-000", "Account Name": "Allocation Overhead",
         "Reporting Code": "360", "Reporting Line": "Allocation overhead",
         "Reporting Account": "360 Allocation overhead"},
    ])
    logic = pd.DataFrame(columns=["Account Code", "Account Name", "PC", "Distribution", "Code"])
    alloc = pd.DataFrame([
        # Roster spans both admin and revenue depts; the sweep drops the admin half.
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Admin One", "Percentage": 0.25},
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Admin Two", "Percentage": 0.25},
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev One", "Percentage": 0.25},
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev Two", "Percentage": 0.25},
    ])
    gl = pd.DataFrame([
        {"Date": "2026-07-05", "Type": "GJ", "Ref No.": "R-1", "Contact": "x",
         "Description": "d", "Note": None, "Dept": "Admin One", "Project": "N/A",
         "Curr": "IDR", "Debit": 100.0, "Credit": 0.0, "Balance": 100.0,
         "Account Code": "6101-00-000", "Account Name": "Salary Expense"},
        {"Date": "2026-07-06", "Type": "GJ", "Ref No.": "R-2", "Contact": "x",
         "Description": "d", "Note": None, "Dept": "Rev One", "Project": "N/A",
         "Curr": "IDR", "Debit": 50.0, "Credit": 0.0, "Balance": 50.0,
         "Account Code": "6101-00-000", "Account Name": "Salary Expense"},
    ])
    return {"PC": pc, "COA": coa, "LOGIC": logic, "ALLOCATION": alloc, "GL": gl}


def _base(cfg, sheets):
    """Run extract-free: lookups -> resolve -> distribute -> enrich."""
    lk = P.build_lookups(cfg, sheets)
    resolved = P.resolve_rule(cfg, sheets["GL"], lk)
    children, rejects = P.distribute(cfg, resolved, lk)
    return lk, resolved, P.enrich(children, lk), rejects


# --- period helpers --------------------------------------------------------
@pytest.mark.parametrize("period,expected", [
    ("JUL-2026", "JUN-2026"), ("JAN-2026", "DEC-2025"), ("2026-03", "FEB-2026"),
])
def test_previous_period(period, expected):
    assert previous_period(period) == expected


def test_month_name_id_is_indonesian():
    assert month_name_id("JUN-2026") == "Juni"
    assert month_name_id("DEC-2025") == "Desember"


# --- COA reporting columns -------------------------------------------------
def test_reporting_columns_come_from_coa(cfg, sheets):
    _, resolved, _, _ = _base(cfg, sheets)
    assert set(resolved["Reporting Code"]) == {"200"}
    assert set(resolved["Reporting Account"]) == {"200 Wages and salaries"}
    assert set(resolved["Reporting Account Name"]) == {"Wages and salaries"}


def test_reporting_columns_fall_back_when_coa_lacks_them(cfg, sheets):
    """A V.04-shaped COA (no Reporting Code/Account) must still resolve."""
    sheets["COA"] = sheets["COA"].drop(columns=["Reporting Code", "Reporting Account"])
    _, resolved, _, _ = _base(cfg, sheets)
    assert resolved["Reporting Code"].isna().all()
    # Label degrades to the reporting line alone rather than blowing up.
    assert set(resolved["Reporting Account"]) == {"Wages and salaries"}


# --- overhead weights ------------------------------------------------------
def test_overhead_weights_exclude_swept_depts_and_renormalise(cfg, sheets):
    lk = P.build_lookups(cfg, sheets)
    w = P.overhead_weights(cfg, lk)
    assert set(w.index) == {"Rev One", "Rev Two"}       # admin depts dropped
    assert w.to_dict() == {"Rev One": 0.5, "Rev Two": 0.5}  # 0.25/0.5 renormalised
    assert w.sum() == pytest.approx(1.0)


def test_overhead_weights_absorb_rounding_residual(cfg, sheets):
    """Three equal receivers round to 0.3333 each; the 0.0001 gap must be closed."""
    sheets["PC"] = pd.concat([sheets["PC"], pd.DataFrame([
        {"Dept Code": "R3", "Dept": "Rev Three", "Div": "Rev Three", "PC": _REV}])],
        ignore_index=True)
    sheets["ALLOCATION"] = pd.DataFrame([
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": d, "Percentage": 1 / 3}
        for d in ("Rev One", "Rev Two", "Rev Three")])
    lk = P.build_lookups(cfg, sheets)
    w = P.overhead_weights(cfg, lk)
    assert w.sum() == pytest.approx(1.0, abs=1e-12)
    assert sorted(w.round(4)) == [0.3333, 0.3333, 0.3334]


def test_overhead_weights_missing_basis_method_raises(cfg, sheets):
    sheets["ALLOCATION"] = sheets["ALLOCATION"].iloc[0:0]
    lk = P.build_lookups(cfg, sheets)
    with pytest.raises(ValueError, match="FTE - All"):
        P.overhead_weights(cfg, lk)


# --- overhead sweep --------------------------------------------------------
def test_sweep_reverses_admin_cost_and_charges_receivers(cfg, sheets):
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)

    assert len(oh) == 3                                  # 1 reversal + 2 receivers
    assert oh["Allocation"].sum() == pytest.approx(0.0)  # zero-sum

    rev = oh[oh["Percentage"] == 1.0]
    assert rev["New Dept"].tolist() == ["Admin One"]
    assert rev["Allocation"].iloc[0] == pytest.approx(-100.0)   # the whole admin cost
    assert rev["Credit"].iloc[0] == pytest.approx(100.0)
    assert rev["Debit"].iloc[0] == pytest.approx(0.0)

    recv = oh[oh["Percentage"] != 1.0].set_index("New Dept")
    assert recv["Allocation"].to_dict() == {"Rev One": 50.0, "Rev Two": 50.0}
    assert (recv["Amount"] == 100.0).all()               # every receiver carries the pool


def test_sweep_metadata_matches_the_workbook_convention(cfg, sheets):
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)
    row = oh.iloc[0]
    assert row["Code"] == cfg.overhead_account_code
    assert row["Account"] == "7501-00-000 Allocation Overhead"
    assert row["Reporting Code"] == "360"                # from COA, not the legacy 660
    assert row["Description"] == "Allocation Overhead 0726"
    assert row["Distribution And Allocation"] == "FTE : PC Juni"   # prior month, Indonesian
    assert str(row["Date"]) == "2026-07-01"              # anchored at the 1st
    assert pd.isna(row[P._GL_ID])                        # derived, not a GL line
    # Receiving depts get their Div/PC from the master, like any other row.
    assert set(oh["PC"]) == {_ADMIN, _REV}


def test_sweep_disabled_returns_nothing(cfg, sheets):
    lk, _, base, _ = _base(cfg, sheets)
    off = Config(input_path=cfg.input_path, overhead_enabled=False)
    assert len(P.sweep_overhead(off, base, lk)) == 0


def test_sweep_skips_period_with_no_admin_cost(cfg, sheets):
    """GL touching only revenue depts leaves nothing to sweep."""
    sheets["GL"] = sheets["GL"].iloc[[1]].reset_index(drop=True)
    lk, _, base, _ = _base(cfg, sheets)
    assert len(P.sweep_overhead(cfg, base, lk)) == 0


def test_validate_accepts_the_zero_sum_sweep(cfg, sheets):
    lk, resolved, base, rejects = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)
    recon = P.validate(cfg, resolved, base, rejects, lk, oh)
    assert recon.ok
    assert recon.n_overhead == 3
    assert recon.overhead_pool == pytest.approx(100.0)
    assert recon.allocated_total == pytest.approx(recon.source_total)
    assert recon.n_output_rows == len(base) + 3


def test_validate_rejects_a_sweep_that_moves_the_total(cfg, sheets):
    lk, resolved, base, rejects = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)
    oh.loc[oh.index[0], "Allocation"] += 1.0          # break the zero-sum property
    with pytest.raises(ValueError, match="not zero-sum"):
        P.validate(cfg, resolved, base, rejects, lk, oh)


# --- output contract -------------------------------------------------------
def test_final_frame_uses_the_workbook_headers(cfg, sheets):
    _, _, base, _ = _base(cfg, sheets)
    final = P._final_frame(base)
    assert len(final.columns) == len(OUTPUT_COLUMNS) == 22
    assert list(final.columns)[:1] == ["date"]                    # not "Date"
    assert "Dept_div" in final.columns and "Div" not in final.columns
    assert list(final.columns).index("Reporting Code") == 9
    assert list(final.columns).index("Reporting Account") == 11


# --- basis map: which method derives from which source ---------------------
def test_basis_map_covers_every_method_exactly_once(cfg):
    """FTE-driven and REV-driven maps must not overlap; the rest are Fixed."""
    fte, rev = set(cfg.fte_scopes), set(cfg.rev_method_column)
    assert not (fte & rev)
    assert fte == {"FTE - All", "FTE - Head Office", "FTE - Laboratory",
                   "FTE - Surabaya", "FTE - Medan",
                   "Head Office : Tower G", "Head Office : Tower B"}
    assert rev == {"Revenue HO", "Revenue", "Revenue - System Certification Services"}


def test_basis_of_labels_every_method(cfg):
    from cost_distribution.basis import basis_of
    assert basis_of(cfg, "FTE - Laboratory") == "basis_fte"
    assert basis_of(cfg, "Head Office : Tower B") == "basis_fte"
    assert basis_of(cfg, "Revenue HO") == "basis_rev"
    assert basis_of(cfg, "Revenue - System Certification Services") == "basis_rev"
    assert basis_of(cfg, "Lab Distribution") == "Fixed"
    assert basis_of(cfg, "PE Product Distribution") == "Fixed"
    # Unknown / renamed methods fall back to Fixed rather than blowing up.
    assert basis_of(cfg, "Something New") == "Fixed"


def test_recompute_stamps_the_basis_column(cfg, sheets):
    from cost_distribution.basis import recompute_allocation
    sheets["ALLOCATION"] = pd.concat([sheets["ALLOCATION"], pd.DataFrame([
        {"Distribution": "Lab Distribution", "Account Name": "Lab Chemical Material",
         "New Dept": "Rev One", "Percentage": 1.0}])], ignore_index=True)
    fte = pd.DataFrame([{"HC": 1, "Dept": "Rev One", "Location": "Head Office",
                         "Location Detail": "Head Office : Tower G"}])
    rev = pd.DataFrame([{"Div": "Rev One", "Percentage HO": 1.0,
                         "Percentage Certification Services": 1.0, "Percentage All": 1.0}])
    out = recompute_allocation(cfg, sheets["ALLOCATION"], fte, rev)
    labels = dict(zip(out["Distribution"], out["Basis"]))
    assert labels["FTE - All"] == "basis_fte"
    assert labels["Lab Distribution"] == "Fixed"
    assert out["Basis"].notna().all()


def test_tower_scopes_use_location_detail(cfg):
    """`Location` only reaches "Head Office" — the towers need the detail column."""
    for method in ("Head Office : Tower G", "Head Office : Tower B"):
        column, token = cfg.fte_scopes[method]
        assert column == "Location Detail"
        assert token == method


def test_fte_factors_are_headcount_share_within_scope(cfg):
    fte = pd.DataFrame([
        {"HC": 3, "Dept": "Rev One", "Location": "Head Office",
         "Location Detail": "Head Office : Tower G"},
        {"HC": 1, "Dept": "Rev Two", "Location": "Head Office",
         "Location Detail": "Head Office : Tower G"},
        {"HC": 9, "Dept": "Rev Two", "Location": "Laboratory",
         "Location Detail": "Laboratory"},          # out of the Tower G scope
    ])
    from cost_distribution.basis import recompute_fte_factors
    roster = {"Head Office : Tower G": ["Rev One", "Rev Two", "Absent Dept"]}
    out = recompute_fte_factors(cfg, fte, roster)
    got = dict(zip(out["New Dept"], out["Percentage"]))
    assert got == {"Rev One": 0.75, "Rev Two": 0.25, "Absent Dept": 0.0}


def test_fte_scope_on_missing_column_raises(cfg):
    fte = pd.DataFrame([{"HC": 1, "Dept": "Rev One", "Location": "Head Office"}])
    from cost_distribution.basis import recompute_fte_factors
    with pytest.raises(ValueError, match="Location Detail"):
        recompute_fte_factors(cfg, fte, {"Head Office : Tower G": ["Rev One"]})


def _fte_rev(cfg, sheets, headcount):
    """FTE register with `headcount` employees in Rev One, 1 in Rev Two."""
    fte = pd.DataFrame(
        [{"HC": 1, "Dept": "Rev One", "Location": "Head Office",
          "Location Detail": "Head Office"}] * headcount
        + [{"HC": 1, "Dept": "Rev Two", "Location": "Head Office",
            "Location Detail": "Head Office"}])
    rev = pd.DataFrame([
        {"Div": "Rev One", "Percentage HO": 0.5,
         "Percentage Certification Services": 0.5, "Percentage All": 0.5},
        {"Div": "Rev Two", "Percentage HO": 0.5,
         "Percentage Certification Services": 0.5, "Percentage All": 0.5}])
    return fte, rev


def test_drift_report_flags_a_stale_method(cfg, sheets):
    """Editing the FTE register must show up as drift until ALLOCATION is rebuilt."""
    from cost_distribution.basis import recompute_allocation, drift_by_method
    # ALLOCATION stored on a 1:1 split...
    sheets["ALLOCATION"] = pd.DataFrame([
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev One", "Percentage": 0.5},
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev Two", "Percentage": 0.5},
    ])
    # ...while the register now says 3:1.
    fte, rev = _fte_rev(cfg, sheets, headcount=3)
    refreshed = recompute_allocation(cfg, sheets["ALLOCATION"], fte, rev)
    report = drift_by_method(cfg, refreshed, sheets["ALLOCATION"]).set_index("method")

    assert not report.loc["FTE - All", "in_sync"]
    assert report.loc["FTE - All", "max_drift"] == pytest.approx(0.25)   # 0.5 vs 0.75
    assert report.loc["FTE - All", "basis"] == "basis_fte"


def test_drift_report_is_clean_once_rebuilt(cfg, sheets):
    from cost_distribution.basis import recompute_allocation, drift_by_method
    sheets["ALLOCATION"] = pd.DataFrame([
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev One", "Percentage": 0.5},
        {"Distribution": "FTE - All", "Account Name": None, "New Dept": "Rev Two", "Percentage": 0.5},
    ])
    fte, rev = _fte_rev(cfg, sheets, headcount=3)
    rebuilt = recompute_allocation(cfg, sheets["ALLOCATION"], fte, rev)
    # Comparing the rebuilt basis against itself is what "in sync" means.
    report = drift_by_method(cfg, rebuilt, rebuilt).set_index("method")
    assert report["in_sync"].all()
    assert report.loc["FTE - All", "sum_stored"] == pytest.approx(1.0)


def test_fixed_methods_never_drift(cfg, sheets):
    """A hand-maintained split must pass through the recompute untouched."""
    from cost_distribution.basis import recompute_allocation, drift_by_method
    sheets["ALLOCATION"] = pd.concat([sheets["ALLOCATION"], pd.DataFrame([
        {"Distribution": "Corporate Services", "Account Name": None,
         "New Dept": "Rev One", "Percentage": 1.0}])], ignore_index=True)
    fte, rev = _fte_rev(cfg, sheets, headcount=3)
    refreshed = recompute_allocation(cfg, sheets["ALLOCATION"], fte, rev)
    report = drift_by_method(cfg, refreshed, sheets["ALLOCATION"]).set_index("method")
    assert report.loc["Corporate Services", "basis"] == "Fixed"
    assert report.loc["Corporate Services", "max_drift"] == 0.0


@pytest.mark.skipif(not os.path.exists(load_config().input_path),
                    reason="V.05 workbook not present")
def test_recompute_reproduces_the_workbook_allocation():
    """Every derived method must rebuild from FTE/REV to the workbook's factors."""
    from cost_distribution.basis import recompute_allocation, verify_against
    cfg = load_config()
    xl = pd.ExcelFile(cfg.input_path, engine="openpyxl")
    alloc, fte, rev = (xl.parse(s) for s in ("ALLOCATION", "FTE", "REV"))
    refreshed = recompute_allocation(cfg, alloc, fte, rev)
    assert verify_against(cfg, refreshed, alloc) == pytest.approx(0.0, abs=1e-9)
    assert len(refreshed) == len(alloc)
    # Fixed methods survive untouched.
    fixed = refreshed[~refreshed["Distribution"].isin(
        set(cfg.fte_scopes) | set(cfg.rev_method_column))]
    assert set(fixed["Distribution"]) == {
        "Lab Distribution", "Corporate Services",
        "Surabaya Distribution", "PE Product Distribution"}


# --- GL sourced from accounting.gl -----------------------------------------
@pytest.fixture
def accounting_engine():
    """In-memory stand-in for the accounting DB's ``gl`` table."""
    import sqlalchemy as sa
    engine = sa.create_engine("sqlite:///:memory:")
    md = sa.MetaData()
    gl = sa.Table(
        "gl", md,
        sa.Column("date", sa.Date), sa.Column("type", sa.String(50)),
        sa.Column("ref_no", sa.String(100)), sa.Column("contact", sa.String(200)),
        sa.Column("description", sa.Text), sa.Column("note", sa.Text),
        sa.Column("department", sa.String(200)), sa.Column("dept_code", sa.String(50)),
        sa.Column("project", sa.String(200)),
        sa.Column("currency", sa.String(10)),
        sa.Column("debit", sa.Numeric(28, 12)), sa.Column("credit", sa.Numeric(28, 12)),
        sa.Column("coa_code", sa.String(50)), sa.Column("coa_name", sa.String(200)),
        sa.Column("reporting", sa.String(50)), sa.Column("status", sa.String(20)),
    )
    dept = sa.Table(
        "department", md,
        sa.Column("dept_code", sa.String(50), primary_key=True),
        sa.Column("dept_name", sa.String(200)),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        # The master is what names a department; the callback's own name is not
        # trusted, so the importer resolves through this table.
        conn.execute(dept.insert(), [
            {"dept_code": "R1", "dept_name": "Rev One"},
            {"dept_code": "R2", "dept_name": "Rev Two"},
            {"dept_code": "A1", "dept_name": "Admin One"},
        ])

    def row(**kw):
        base = dict(date=date(2026, 7, 5), type="GJ", ref_no="R-1", contact="c",
                    description="d", note=None, department="Rev One",
                    dept_code="R1", project="N/A",
                    currency="IDR", debit=100.0, credit=0.0, coa_code="6101-00-000",
                    coa_name="Salary Expense", reporting="Profit & Loss",
                    status="approved")
        base.update(kw)
        return base

    with engine.begin() as conn:
        conn.execute(gl.insert(), [
            row(),                                                   # kept
            row(debit=50.0, coa_code="5220-10-000", coa_name="Product Testing"),  # kept
            row(debit=7.0, coa_code="7501-00-000", coa_name="Allocation Overhead"),  # kept
            row(debit=999.0, coa_code="1112-11-000", coa_name="HSBC",
                reporting="Balance Sheet"),                          # dropped: not P&L
            row(debit=0.0, credit=888.0, coa_code="4111-10-000",
                coa_name="Certificate"),                             # kept: revenue
            row(debit=777.0, coa_code="9104-00-000", coa_name="Forex Loss"),      # dropped: 9xxx
            row(debit=666.0, status="draft"),                        # dropped: status
            row(debit=11.0, department="CERTIFICATION SERVICES"),    # kept + dept renamed
            row(debit=13.0, coa_code="6918-00-000",
                coa_name="Internal Events, Meeting and Briefing"),   # kept + account renamed
            row(date=date(2026, 8, 3), debit=555.0),                 # dropped: other month
        ])
    yield engine
    engine.dispose()


@pytest.fixture
def cost_engine():
    import sqlalchemy as sa
    from models.cost_distribution import CostDistributionBase
    engine = sa.create_engine("sqlite:///:memory:")
    CostDistributionBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_import_gl_from_accounting_keeps_the_pl_base(cfg, accounting_engine, cost_engine):
    from load.cost_distribution_gl import import_gl_from_accounting
    rep = import_gl_from_accounting(cfg, "JUL-2026", cost_engine, accounting_engine)

    assert rep["imported"] == 6                     # 4 cost + 1 dept-renamed + 1 revenue
    assert rep["cost"] == pytest.approx(100 + 50 + 7 + 11 + 13)
    assert rep["revenue"] == pytest.approx(888)     # reported apart from cost, not netted
    assert rep["source_rows"] == 9                  # August row is outside the period query
    # The alias map no longer fires: the department master resolves the name
    # from the code before any alias would apply.
    assert rep["dept_renamed"] == 0
    assert rep["account_renamed"] == 1

    ex = rep["excluded"]
    assert ex["not_profit_and_loss"][0] == 1        # balance sheet
    assert ex["status_not_allowed"][0] == 1         # draft
    assert ex["outside_cost_accounts"][0] == 1      # 9xxx forex loss only


def test_import_gl_from_accounting_canonicalises_names(cfg, accounting_engine, cost_engine):
    from load.cost_distribution_gl import import_gl_from_accounting, read_gl_from_db
    import_gl_from_accounting(cfg, "JUL-2026", cost_engine, accounting_engine)
    gl = read_gl_from_db("JUL-2026", cost_engine)

    # The department master names the row, keyed on its code — the callback's
    # own department name is not trusted, because Zahir keeps sending the
    # pre-rename one.
    assert "CERTIFICATION SERVICES" not in set(gl["Dept"])
    assert set(gl["Dept"]) == {"Rev One"}
    assert "Internal Events, Meeting and Briefing" not in set(gl["Account Name"])
    assert "Internal Events" in set(gl["Account Name"])
    # Balance restates the signed amount, the shape the pipeline expects.
    assert (gl["Balance"] == gl["Debit"] - gl["Credit"]).all()


def test_import_gl_from_accounting_replaces_the_period(cfg, accounting_engine, cost_engine):
    """Re-importing must not accumulate duplicate rows."""
    from load.cost_distribution_gl import import_gl_from_accounting
    first = import_gl_from_accounting(cfg, "JUL-2026", cost_engine, accounting_engine)
    again = import_gl_from_accounting(cfg, "JUL-2026", cost_engine, accounting_engine)
    assert first["imported"] == again["imported"]
    from sqlalchemy import text as _t
    with cost_engine.connect() as c:
        assert c.execute(_t("SELECT COUNT(*) FROM gl_entry")).scalar() == again["imported"]


def test_import_gl_from_accounting_empty_period_raises(cfg, accounting_engine, cost_engine):
    from load.cost_distribution_gl import import_gl_from_accounting
    with pytest.raises(ValueError, match="no rows"):
        import_gl_from_accounting(cfg, "JAN-2026", cost_engine, accounting_engine)


# --- automatic created_at / updated_at -------------------------------------
_STAMPED_TABLES = ["basis_pc", "basis_coa", "basis_logic", "basis_rl",
                   "basis_allocation", "basis_fte", "basis_rev", "gl_entry"]


def test_stamp_columns_are_not_null_with_a_server_default(cost_engine):
    """Every basis table must be able to stamp itself, without the caller's help."""
    from sqlalchemy import inspect
    insp = inspect(cost_engine)
    for table in _STAMPED_TABLES:
        cols = {c["name"]: c for c in insp.get_columns(table)}
        for name in ("created_at", "updated_at"):
            assert name in cols, f"{table}.{name} missing"
            assert not cols[name]["nullable"], f"{table}.{name} should be NOT NULL"
            assert cols[name]["default"] is not None, f"{table}.{name} needs a server default"


def test_insert_without_stamps_is_filled_by_the_database(cost_engine):
    """A hand-written INSERT that omits the stamps must still get them."""
    from sqlalchemy import text as _t
    with cost_engine.begin() as c:
        c.execute(_t("INSERT INTO basis_pc (dept_code, dept, div, pc) "
                     "VALUES ('ZZ9', 'Test', 'Test', 'Test')"))
        row = c.execute(_t("SELECT created_at, updated_at FROM basis_pc "
                           "WHERE dept_code='ZZ9'")).first()
    assert row[0] is not None and row[1] is not None


def test_orm_update_restamps_updated_at(cost_engine):
    """On backends without the trigger, the ORM's onupdate still moves the stamp."""
    from sqlalchemy.orm import sessionmaker
    from models.cost_distribution import BasisPC
    from datetime import datetime, timedelta
    Session = sessionmaker(bind=cost_engine, future=True)
    old = datetime(2020, 1, 1)
    with Session() as s:
        s.add(BasisPC(dept_code="ZZ8", dept="T", div="T", pc="T",
                      created_at=old, updated_at=old))
        s.commit()
        row = s.query(BasisPC).filter_by(dept_code="ZZ8").one()
        row.div = "changed"
        s.commit()
        s.refresh(row)
        assert row.updated_at > old
        assert row.created_at == old        # creation time is never rewritten
        assert datetime.utcnow() - row.updated_at < timedelta(minutes=5)


# --- period lock -----------------------------------------------------------
def _snapshot(cfg, cost_engine, period=None):
    """Minimal APR-2026 output + recon, loaded through the real DB path."""
    from load.cost_distribution_db import load_to_db
    out = pd.DataFrame([{
        "Date": date(2026, 4, 3), "Code": "6101-00-000", "Account Name": "Salary",
        "Dept": "Rev One", "New Dept": "Rev One", "Div": "Rev One", "PC": "X",
        "Debit": 10.0, "Credit": 0.0, "Amount": 10.0, "Percentage": 1.0,
        "Allocation": 10.0, "period": "APR-2026", "gl_line_id": 0, "_method": None,
    }])
    recon = P.Recon(source_total=10.0, allocated_total=10.0, variance=0.0,
                    n_gl_lines=1, n_output_rows=1, n_direct=1, n_distributed=0,
                    n_rejects=0, n_overhead=0, overhead_pool=0.0, ok=True)
    return load_to_db(out, recon, cfg, engine=cost_engine, period=period)


def test_snapshot_load_refuses_a_closed_period_even_without_period_arg(cfg, cost_engine):
    """The lock must gate on the months written, not on the --period argument.

    Invoked with period=None the caller-level check sees nothing to check, so
    the guard has to live where the rows are known.
    """
    from load.cost_distribution_period import close_period, PeriodClosedError
    from sqlalchemy import text as _t
    close_period(cost_engine, "APR-2026")

    with pytest.raises(PeriodClosedError, match="2026-04-01"):
        _snapshot(cfg, cost_engine, period=None)

    # Nothing partially written — the whole load rolls back.
    with cost_engine.connect() as c:
        assert c.execute(_t("SELECT COUNT(*) FROM distribution")).scalar() == 0
        assert c.execute(_t("SELECT COUNT(*) FROM distribution_run")).scalar() == 0


def test_snapshot_load_allows_an_open_period(cfg, cost_engine):
    from sqlalchemy import text as _t
    assert _snapshot(cfg, cost_engine, period="APR-2026")
    with cost_engine.connect() as c:
        assert c.execute(_t("SELECT COUNT(*) FROM distribution")).scalar() == 1


def test_reopening_restores_writability(cfg, cost_engine):
    from load.cost_distribution_period import close_period, reopen_period, PeriodClosedError
    close_period(cost_engine, "APR-2026")
    with pytest.raises(PeriodClosedError):
        _snapshot(cfg, cost_engine, period=None)
    assert reopen_period(cost_engine, "APR-2026") is True
    assert _snapshot(cfg, cost_engine, period=None)


# --- reconciliation against the real workbook ------------------------------
@pytest.mark.skipif(not os.path.exists(load_config().input_path),
                    reason="V.05 workbook not present")
def test_reconciles_to_the_v05_workbook():
    """Full run must reproduce the workbook's Distribution sheet, to the cent."""
    # The workbook books its revenue block as a positive debit, so the
    # credit-balance rule (a later policy) is switched off to compare like
    # with like — this test is about the engine, not the sign convention.
    cfg = replace(load_config(), credit_balance_prefixes=())
    out, rejects, recon = P.run(cfg, dry_run=True, period="JUL-2026")
    wb = pd.read_excel(cfg.input_path, cfg.sheet_distribution)
    wb = wb[wb["date"].notna()]

    assert recon.ok and not len(rejects)
    assert len(out) == len(wb) == 3047
    assert list(P._final_frame(out).columns) == list(wb.columns)
    assert round(float(out["Allocation"].sum()), 2) == round(float(wb["Allocation"].sum()), 2)

    # The overhead block: same 31 rows, same pool, still zero-sum.
    oh = out[out[P._GL_ID].isna()]
    assert len(oh) == 31
    assert recon.overhead_pool == pytest.approx(4019660640.52, abs=0.01)
    assert float(oh["Allocation"].sum()) == pytest.approx(0.0, abs=0.01)

    # Every (line, receiving dept) figure agrees with the workbook.
    def by_dept(df, col):
        return df.groupby(df[col].astype(str).str.strip())["Allocation"].sum().round(2)
    ours, theirs = by_dept(out, "New Dept"), by_dept(wb, "New Dept")
    assert ours.index.equals(theirs.index)
    assert (ours - theirs).abs().max() < 0.01


# --- revenue alongside cost ------------------------------------------------
def _with_revenue(sheets):
    """Add a revenue account and a revenue GL line to the fixture."""
    sheets["COA"] = pd.concat([sheets["COA"], pd.DataFrame([
        {"Code": "4125-10-000", "Account Name": "CTS Lab", "Reporting Code": "002",
         "Reporting Line": "Sales revenues", "Reporting Account": "002 Sales revenues"}])],
        ignore_index=True)
    sheets["GL"] = pd.concat([sheets["GL"], pd.DataFrame([
        {"Date": "2026-07-07", "Type": "SI", "Ref No.": "INV-9", "Contact": "x",
         "Description": "sale", "Note": None, "Dept": "Rev One", "Project": "N/A",
         "Curr": "IDR", "Debit": 0.0, "Credit": 400.0, "Balance": -400.0,
         "Account Code": "4125-10-000", "Account Name": "CTS Lab"}])],
        ignore_index=True)
    return sheets


def test_revenue_amount_is_credit_minus_debit(cfg, sheets):
    """A credit-balance account reads positive, the mirror of how cost reads."""
    _, resolved, _, _ = _base(cfg, _with_revenue(sheets))
    rev = resolved[resolved["Account Code"] == "4125-10-000"].iloc[0]
    assert rev["Amount"] == pytest.approx(400.0)          # credit - debit
    cost = resolved[resolved["Account Code"] == "6101-00-000"].iloc[0]
    assert cost["Amount"] == pytest.approx(100.0)         # debit - credit, unchanged


def test_revenue_reversal_stays_negative(cfg, sheets):
    """A credit note reduces revenue, so it must not be forced positive."""
    sheets = _with_revenue(sheets)
    sheets["GL"].loc[sheets["GL"]["Ref No."] == "INV-9", "Credit"] = -400.0
    _, resolved, _, _ = _base(cfg, sheets)
    rev = resolved[resolved["Account Code"] == "4125-10-000"].iloc[0]
    assert rev["Amount"] == pytest.approx(-400.0)


def test_revenue_is_charged_direct_to_its_department(cfg, sheets):
    """No LOGIC rule covers revenue, so it stays with the dept that earned it."""
    _, _, base, _ = _base(cfg, _with_revenue(sheets))
    rev = base[base["Code"] == "4125-10-000"]
    assert len(rev) == 1
    assert rev.iloc[0]["New Dept"] == "Rev One"
    assert rev.iloc[0]["Percentage"] == 1.0
    assert pd.isna(rev.iloc[0][P._METHOD])


def test_sweep_pool_is_a_net_position_not_a_cost_total(cfg, sheets):
    """A revenue-generating department's pool is unaffected: only admin depts
    are swept, so revenue elsewhere never enters the pool at all."""
    sheets = _with_revenue(sheets)          # revenue sits on Rev One
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)
    swept = oh[oh["Percentage"] == 1.0]
    assert swept["New Dept"].tolist() == ["Admin One"]
    assert swept["Allocation"].iloc[0] == pytest.approx(-100.0)
    assert oh["Allocation"].sum() == pytest.approx(0.0)


def test_is_credit_balance_follows_config(cfg):
    codes = pd.Series(["4125-10-000", "5211-00-000", "6101-00-000", None])
    flags = P.is_credit_balance(cfg, codes).tolist()
    assert flags == [True, False, False, False]


def test_admin_revenue_nets_against_cost_before_the_sweep(cfg, sheets):
    """Revenue earned by an admin dept settles its own cost first.

    Admin One carries 100 of cost and earns 30; only the net 70 is pushed out to
    the revenue-generating departments.
    """
    sheets = _with_revenue(sheets)
    sheets["GL"].loc[sheets["GL"]["Ref No."] == "INV-9", ["Dept", "Credit"]] = ["Admin One", 30.0]
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)

    reversal = oh[oh["Percentage"] == 1.0]
    assert reversal["New Dept"].tolist() == ["Admin One"]
    assert reversal["Allocation"].iloc[0] == pytest.approx(-70.0)   # 100 cost - 30 revenue

    recv = oh[oh["Percentage"] != 1.0].set_index("New Dept")["Allocation"]
    assert recv.to_dict() == {"Rev One": 35.0, "Rev Two": 35.0}
    assert oh["Allocation"].sum() == pytest.approx(0.0)


def test_admin_revenue_exceeding_cost_reverses_the_sweep(cfg, sheets):
    """Earning more than it spends leaves an admin dept in credit, and the
    sweep hands that surplus out as a negative charge rather than breaking."""
    sheets = _with_revenue(sheets)
    sheets["GL"].loc[sheets["GL"]["Ref No."] == "INV-9", ["Dept", "Credit"]] = ["Admin One", 250.0]
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)

    reversal = oh[oh["Percentage"] == 1.0]
    assert reversal["Allocation"].iloc[0] == pytest.approx(150.0)   # -(100 - 250)
    recv = oh[oh["Percentage"] != 1.0].set_index("New Dept")["Allocation"]
    assert recv.to_dict() == {"Rev One": -75.0, "Rev Two": -75.0}
    assert oh["Allocation"].sum() == pytest.approx(0.0)


def test_a_fixed_rule_routes_every_bucket_to_one_department(cfg, sheets):
    """The Bank Administrative shape: same account, any bucket, one destination."""
    sheets["LOGIC"] = pd.DataFrame([
        {"Account Code": "6924-00-000", "Account Name": "Bank Administrative",
         "PC": bucket, "Distribution": "Bank Administrative", "Code": "6924/Bank"}
        for bucket in (_ADMIN, _REV)])
    sheets["ALLOCATION"] = pd.concat([sheets["ALLOCATION"], pd.DataFrame([
        {"Distribution": "Bank Administrative", "Account Name": None,
         "New Dept": "Rev Two", "Percentage": 1.0}])], ignore_index=True)
    sheets["COA"] = pd.concat([sheets["COA"], pd.DataFrame([
        {"Code": "6924-00-000", "Account Name": "Bank Administrative",
         "Reporting Code": "024", "Reporting Line": "Other operating costs",
         "Reporting Account": "024 Other operating costs"}])], ignore_index=True)
    # Two source departments, in different buckets.
    sheets["GL"] = pd.concat([sheets["GL"], pd.DataFrame([
        {"Date": "2026-07-08", "Type": "GJ", "Ref No.": "B-1", "Contact": "x",
         "Description": "bank", "Note": None, "Dept": dept, "Project": "N/A",
         "Curr": "IDR", "Debit": amount, "Credit": 0.0, "Balance": amount,
         "Account Code": "6924-00-000", "Account Name": "Bank Administrative"}
        for dept, amount in (("Admin One", 20.0), ("Rev One", 5.0))])],
        ignore_index=True)

    _, _, base, rejects = _base(cfg, sheets)
    assert not len(rejects)
    bank = base[base["Code"] == "6924-00-000"]
    assert len(bank) == 2
    assert set(bank["New Dept"]) == {"Rev Two"}          # both, whatever the bucket
    assert bank["Allocation"].sum() == pytest.approx(25.0)
    assert set(bank["Dept"]) == {"Admin One", "Rev One"}  # origin still recorded


# --- department code beats a stale name ------------------------------------
def _pc_with_codes(sheets):
    """The fixture's PC master already carries codes; this just names the intent."""
    assert "Dept Code" in sheets["PC"].columns
    return sheets


def test_bucket_prefers_the_department_code_over_a_renamed_name(cfg, sheets):
    """A row whose stored name no longer matches its code follows the code."""
    sheets = _pc_with_codes(sheets)
    # The line says "Rev One" but carries Admin One's code.
    sheets["GL"].loc[0, "Dept"] = "Rev One"
    sheets["GL"]["Dept Code"] = ["A1", "R1"]
    lk = P.build_lookups(cfg, sheets)
    resolved = P.resolve_rule(cfg, sheets["GL"], lk)
    assert resolved.loc[0, P._BUCKET] == _ADMIN        # from the code, not the name


def test_bucket_falls_back_to_the_name_when_no_code_is_supplied(cfg, sheets):
    """The sales_detail feed carries no code; it must still resolve."""
    sheets = _pc_with_codes(sheets)
    sheets["GL"]["Dept Code"] = [None, None]
    lk = P.build_lookups(cfg, sheets)
    resolved = P.resolve_rule(cfg, sheets["GL"], lk)
    assert resolved.loc[0, P._BUCKET] == _ADMIN        # "Admin One" by name
    assert resolved.loc[1, P._BUCKET] == _REV


def test_bucket_resolves_a_name_the_pc_master_no_longer_knows(cfg, sheets):
    """The case the code exists for: the name has been retired outright."""
    sheets = _pc_with_codes(sheets)
    sheets["GL"].loc[0, "Dept"] = "Admin One (Old Wording)"
    sheets["GL"]["Dept Code"] = ["A1", "R1"]
    lk = P.build_lookups(cfg, sheets)
    resolved = P.resolve_rule(cfg, sheets["GL"], lk)
    assert resolved.loc[0, P._BUCKET] == _ADMIN        # unresolvable by name alone


def test_pc_master_without_codes_still_works(cfg, sheets):
    """A V.04-shaped PC sheet has no Dept Code column at all."""
    sheets["PC"] = sheets["PC"].drop(columns=["Dept Code"])
    lk = P.build_lookups(cfg, sheets)
    assert lk.code_to_bucket == {}
    resolved = P.resolve_rule(cfg, sheets["GL"], lk)
    assert resolved.loc[0, P._BUCKET] == _ADMIN


def test_dept_aliases_agree_with_the_department_master():
    """The workbook fallback must rename to the same department the code means.

    The master says D01 is "System Certification Services" and D010 is
    "Integrated Management System" — two different departments. An alias that
    sends the old wording to the wrong one moves money silently.
    """
    cfg = Config(input_path="unused.xlsx")
    assert cfg.dept_aliases["CERTIFICATION SERVICES"] == "System Certification Services"
    assert cfg.dept_aliases[
        "Automotive (System Certification Services Technical Ops)"] == "Automotive"
    # An alias must never point at a name that is itself an alias key.
    assert not (set(cfg.dept_aliases.values()) & set(cfg.dept_aliases))


def test_receiving_pc_follows_the_code_not_a_stale_name(cfg, sheets):
    """The reported bug: a stale department name put cost in another PC.

    Zahir renamed D010, but its callbacks still emit the old name — which is the
    *current* name of a different department, D01. Resolving the receiving
    department by name therefore sent D010's cost into D01's profit centre.
    """
    sheets["PC"] = pd.DataFrame([
        # Two departments; the second's name is the first's former name.
        {"Dept Code": "D010", "Dept": "Integrated Management System",
         "Div": "IMS", "PC": "Auditing Indonesia"},
        {"Dept Code": "D01", "Dept": "System Certification Services",
         "Div": "SCS", "PC": "System Certification Services"},
    ])
    sheets["GL"] = pd.DataFrame([{
        "Date": "2026-07-05", "Type": "GJ", "Ref No.": "R-1", "Contact": "x",
        "Description": "d", "Note": None, "Project": "N/A", "Curr": "IDR",
        "Debit": 100.0, "Credit": 0.0, "Balance": 100.0,
        "Account Code": "6101-00-000", "Account Name": "Salary Expense",
        # The code says D010; the name is stale and reads as D01's.
        "Dept Code": "D010", "Dept": "System Certification Services",
    }])
    _, _, base, _ = _base(cfg, sheets)
    row = base.iloc[0]
    assert row["New Dept Code"] == "D010"
    assert row["PC"] == "Auditing Indonesia"              # not D01's PC
    assert row["New Dept"] == "Integrated Management System"   # shown by its code
    assert row["Div"] == "IMS"


def test_distributed_row_resolves_its_receiving_code_from_allocation(cfg, sheets):
    """ALLOCATION names its receiving departments, so the code is looked up."""
    _, _, base, _ = _base(cfg, sheets)
    recv = base[base[P._METHOD].notna()]
    if len(recv):
        assert recv["New Dept Code"].notna().all()


def test_sweep_rows_carry_a_department_code(cfg, sheets):
    """The sweep invents its rows, so there is no code to inherit from a GL line.

    The department name it uses is one taken from the PC master, so the master
    resolves it straight back — leaving the block coded like every other row.
    """
    lk, _, base, _ = _base(cfg, sheets)
    oh = P.sweep_overhead(cfg, base, lk)
    assert len(oh)
    assert oh["Dept Code"].notna().all()
    assert oh["New Dept Code"].notna().all()
    assert (oh["Dept Code"] == oh["New Dept Code"]).all()   # same department
    assert pd.isna(oh[P._GL_ID]).all()                      # still derived rows

    # The code must name the department the row claims, and its PC.
    reversal = oh[oh["Percentage"] == 1.0].iloc[0]
    assert reversal["Dept Code"] == "A1"                    # Admin One in the fixture
    assert reversal["PC"] == _ADMIN
