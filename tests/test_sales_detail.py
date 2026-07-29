"""Tests for the manually imported ``sales_detail`` module and its journal."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

import pytest

from transforms.sales_detail import (
    AR_DOMESTIC, AR_FOREIGN, VAT_ACCOUNT,
    build_invoice_journal, build_journal, journal_is_balanced,
    receivable_account, unmapped_accounts,
)

# (gl account_code, coa_name), keyed by account name and by dashed COA code —
# the shape load_coa_resolver builds.
RESOLVER = {
    "CTS Lab (Sample Analysis )": ("41251", "CTS Lab (Sample Analysis )"),
    "Certificate Of System (D)": ("41211", "Certificate Of System (D)"),
    AR_DOMESTIC: ("112111", "Domestic A/R"),
    AR_FOREIGN: ("112112", "Foreign A/R"),
    VAT_ACCOUNT: ("212411", "Sales VAT"),
}


def line(**kw):
    base = dict(ref_no="INV-1", date=date(2026, 7, 1), contact="PT Uji",
                dept="CTS Laboratory", description="Testing",
                service_code="306D0401", service="306D0401 CTS (Lab Sample)",
                account="CTS Lab (Sample Analysis )",
                subtotal=Decimal("2178000"), tax=Decimal("239580"),
                total=Decimal("2417580"), status="approved",
                currency="IDR", exchange_rate=Decimal("1"), created_at=None)
    base.update(kw)
    return base


def by_account(rows):
    return {r["account_code"]: r for r in rows}


# --- the receivable account is the only thing currency changes ---------------
@pytest.mark.parametrize("currency,expected", [
    ("IDR", AR_DOMESTIC), ("idr", AR_DOMESTIC), (None, AR_DOMESTIC),
    ("USD", AR_FOREIGN), ("EUR", AR_FOREIGN), ("SGD", AR_FOREIGN),
])
def test_receivable_account_follows_currency(currency, expected):
    assert receivable_account(currency) == expected


def test_idr_invoice_matches_the_worked_example():
    """One service line -> A/R debit, revenue credit, VAT credit."""
    rows = build_invoice_journal([line()], RESOLVER)
    assert len(rows) == 3
    acc = by_account(rows)

    assert acc["112111"]["debit"] == Decimal("2417580")
    assert acc["112111"]["credit"] == Decimal("0")
    assert acc["41251"]["credit"] == Decimal("2178000")
    assert acc["212411"]["credit"] == Decimal("239580")
    assert all(r["module"] == "sales_detail" and r["type"] == "sales_detail" for r in rows)
    assert all(r["ref_key"] == "INV-1" and r["source_id"] == "INV-1" for r in rows)
    assert not journal_is_balanced(rows)


def test_department_comes_from_dept_not_service_name():
    rows = build_invoice_journal([line()], RESOLVER)
    assert {r["department"] for r in rows} == {"CTS Laboratory"}


def test_non_idr_invoice_books_to_foreign_ar():
    rows = build_invoice_journal(
        [line(currency="USD", exchange_rate=Decimal("16000"))], RESOLVER)
    acc = by_account(rows)
    assert "112112" in acc and "112111" not in acc      # Foreign, not Domestic
    assert acc["112112"]["coa_name"] == "Foreign A/R"
    assert all(r["currency"] == "USD" for r in rows)
    # The revenue account is unaffected by currency.
    assert acc["41251"]["credit"] == Decimal("2178000")
    # original_currency restates the row in its own currency.
    assert acc["112112"]["original_currency"] == Decimal("2417580") / Decimal("16000")


def test_multi_line_invoice_posts_one_receivable_and_one_vat():
    rows = build_invoice_journal([
        line(service_code="A", subtotal=Decimal("100"), tax=Decimal("11"),
             total=Decimal("111")),
        line(service_code="B", subtotal=Decimal("200"), tax=Decimal("22"),
             total=Decimal("222"), account="Certificate Of System (D)",
             dept="Integrated Management System"),
    ], RESOLVER)

    assert len(rows) == 4                                   # 1 A/R + 2 revenue + 1 VAT
    assert sum(1 for r in rows if r["account_code"] == "112111") == 1
    assert sum(1 for r in rows if r["account_code"] == "212411") == 1
    acc = by_account(rows)
    assert acc["112111"]["debit"] == Decimal("333")
    assert acc["212411"]["credit"] == Decimal("33")
    # Each revenue line keeps its own department.
    revenue = [r for r in rows if r["account_code"] in ("41251", "41211")]
    assert {r["department"] for r in revenue} == {"CTS Laboratory",
                                                  "Integrated Management System"}
    assert not journal_is_balanced(rows)


def test_receivable_is_derived_not_taken_from_total():
    """Where the sheet's own total disagrees, the entry must still balance."""
    rows = build_invoice_journal(
        [line(subtotal=Decimal("22806565.47"), tax=Decimal("2508722.20"),
              total=Decimal("25315287.68"))],       # 0.01 more than the parts
        RESOLVER)
    acc = by_account(rows)
    assert acc["112111"]["debit"] == Decimal("25315287.67")   # parts win
    assert not journal_is_balanced(rows)


def test_zero_tax_posts_no_vat_row():
    rows = build_invoice_journal(
        [line(tax=Decimal("0"), total=Decimal("2178000"))], RESOLVER)
    assert len(rows) == 2
    assert "212411" not in by_account(rows)
    assert not journal_is_balanced(rows)


def test_amount_and_line_ids_are_deterministic():
    rows = build_invoice_journal([line()], RESOLVER)
    acc = by_account(rows)
    assert acc["112111"]["amount"] == Decimal("2417580")     # debit - credit
    assert acc["41251"]["amount"] == Decimal("-2178000")
    assert acc["112111"]["source_line_id"] == "INV-1#AR"
    assert acc["41251"]["source_line_id"] == "INV-1#306D0401"
    assert acc["212411"]["source_line_id"] == "INV-1#VAT"


def test_build_journal_groups_by_invoice():
    rows = build_journal([line(ref_no="INV-1"), line(ref_no="INV-2")], RESOLVER)
    assert {r["ref_no"] for r in rows} == {"INV-1", "INV-2"}
    assert sum(1 for r in rows if r["account_code"] == "112111") == 2
    assert not journal_is_balanced(rows)


# --- screening -------------------------------------------------------------
def test_unmapped_accounts_are_reported_not_guessed():
    rows = [line(), line(ref_no="INV-2", account="Non-Food Inspection")]
    bad = unmapped_accounts(rows, RESOLVER)
    assert [r["ref_no"] for r in bad] == ["INV-2"]
    # And the journal refuses to invent a code for it.
    with pytest.raises(KeyError):
        build_invoice_journal([rows[1]], RESOLVER)


def test_journal_is_balanced_catches_a_broken_entry():
    rows = build_invoice_journal([line()], RESOLVER)
    rows[1]["credit"] += Decimal("1")
    assert journal_is_balanced(rows) == ["INV-1"]


def test_credit_note_reverses_the_entry():
    """A RETUR/BATAL line is a negative invoice, and must still balance.

    Negative debit for the receivable is this ledger's existing convention for a
    reversal (other modules post the same way), so the entry is a mirror image
    rather than a swap of the debit and credit columns.
    """
    rows = build_invoice_journal(
        [line(ref_no="INV-1 - RETUR", subtotal=Decimal("-9018500"),
              tax=Decimal("-992035"), total=Decimal("-10010535"),
              account="Certificate Of System (D)")],
        RESOLVER)
    acc = by_account(rows)
    assert acc["112111"]["debit"] == Decimal("-10010535")
    assert acc["41211"]["credit"] == Decimal("-9018500")
    assert acc["212411"]["credit"] == Decimal("-992035")
    assert not journal_is_balanced(rows)


def test_receivable_row_is_identifiable_by_line_id():
    """`posting_status` finds the receivable by line id, not by sign — a credit
    note's negative debit must not read as 'never posted'."""
    for total in (Decimal("2417580"), Decimal("-2417580")):
        rows = build_invoice_journal(
            [line(subtotal=total / Decimal("1.11"), tax=Decimal("0"), total=total)],
            RESOLVER)
        ar = [r for r in rows if r["source_line_id"].endswith("#AR")]
        assert len(ar) == 1


# --- department master load ------------------------------------------------
def test_loading_the_listing_keeps_departments_it_does_not_contain():
    """A code added straight to the table must survive a listing reload.

    Departments appear in Zahir before the listing catches up, so the load
    upserts rather than replaces: a blind DELETE would drop those silently, and
    every ledger row carrying that code would stop resolving.
    """
    import sqlalchemy as sa
    from load.department_loader import load_departments
    from models.finance import Department

    engine = sa.create_engine("sqlite:///:memory:")
    Department.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO department (dept_code, dept_name) "
                             "VALUES ('D013', 'CoS - ISO 22000-HACCP')"))

    listing = pd.DataFrame({"DEPARTMENT NO": ["A00", "D041"],
                            "DEPARTMENT NAME": ["Head Quarter", "CTS Laboratory"]})
    path = "/tmp/_dept_listing_test.tsv"
    listing.to_csv(path, sep="\t", index=False)

    # unexplained_codes touches the ledger tables, which this stand-in lacks.
    import load.department_loader as dl
    original, dl._LEDGER_TABLES = dl._LEDGER_TABLES, []
    try:
        report = load_departments(path, engine)
    finally:
        dl._LEDGER_TABLES = original

    with engine.connect() as conn:
        codes = set(conn.execute(sa.text("SELECT dept_code FROM department")).scalars())
    assert codes == {"A00", "D041", "D013"}      # the hand-added one survived
    assert report["surplus"] == ["D013"]         # and was reported, not hidden
