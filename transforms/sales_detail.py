"""Journal for the manually imported ``sales_detail`` module.

Zahir sends no callback for sales invoices, so those documents are imported from
a workbook into ``sales_detail`` and their journal is generated here rather than
parsed from a payload.

One invoice posts one balanced entry, at invoice grain:

===========================================  =======  ======================
Line                                         Side     Amount
===========================================  =======  ======================
Accounts receivable                          debit    Σ ``total``
Revenue, one row per service line            credit   that line's ``subtotal``
Sales VAT                                    credit   Σ ``tax``
===========================================  =======  ======================

The receivable is a single row because an invoice is a single claim on the
customer; revenue stays split per service line so each keeps its own account and
department. **Which** receivable account depends on the currency — a non-IDR
invoice books to Foreign A/R — and that is the only account that varies.

The revenue account itself is whatever ``sales_detail.account`` names, resolved
against ``accounting.coa``. An account name with no match is never guessed at:
it is returned as unmapped so the caller can refuse the import (an unbalanced or
mis-posted journal is far worse than a failed load).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Tuple

# Receivable accounts. Domestic is the default; a non-IDR invoice uses Foreign.
AR_DOMESTIC = "1121-11-000"
AR_FOREIGN = "1121-12-000"
VAT_ACCOUNT = "2124-11-000"

MODULE = "sales_detail"
BASE_CURRENCY = "IDR"


def _dec(value: Any) -> Decimal:
    """Money as Decimal; blanks and unparseable values are zero."""
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def receivable_account(currency: str | None) -> str:
    """Domestic A/R for IDR, Foreign A/R for anything else."""
    code = (currency or BASE_CURRENCY).strip().upper()
    return AR_DOMESTIC if code == BASE_CURRENCY else AR_FOREIGN


def _dominant(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The line an invoice-level row inherits its narrative fields from.

    An invoice's receivable and VAT belong to the document, not to one service,
    but ``gl`` still wants a department and a description on every row. The
    largest line by value is the least arbitrary choice; ``service_code`` breaks
    ties so the result never depends on input ordering.
    """
    return max(lines, key=lambda r: (_dec(r.get("subtotal")), str(r.get("service_code") or "")))


def build_invoice_journal(lines: List[Dict[str, Any]],
                          coa_by_name: Dict[str, Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Return the balanced ``gl`` rows for one invoice's service lines.

    ``coa_by_name`` resolves an account to ``(gl account_code, coa_name)`` and is
    keyed **both** by account name (for the revenue lines, which the workbook
    names) and by dashed COA code (for the fixed A/R and VAT accounts). The code
    it returns is the short form ``gl`` stores — ``112111``, not ``1121-11-000``.

    Raises ``KeyError`` if a line names an account that is not in the chart —
    callers are expected to have screened for that with :func:`unmapped_accounts`.
    """
    if not lines:
        return []

    head = _dominant(lines)
    ref_no = str(head["ref_no"])
    currency = (head.get("currency") or BASE_CURRENCY).strip().upper()
    rate = _dec(head.get("exchange_rate")) or Decimal("1")

    def row(account_code: str, coa_name: str, debit: Decimal, credit: Decimal,
            line_id: str, source: Dict[str, Any]) -> Dict[str, Any]:
        amount = debit - credit
        return {
            "module": MODULE,
            "type": MODULE,
            "ref_key": ref_no,
            "ref_no": ref_no,
            "date": head.get("date"),
            "contact": head.get("contact"),
            "description": source.get("description"),
            "note": source.get("service"),
            "dept_code": source.get("dept_code"),
            "department": source.get("dept"),
            "project": None,
            "debit": debit,
            "credit": credit,
            "amount": amount,
            "account_code": account_code,
            "coa_name": coa_name,
            "source_id": ref_no,
            "source_line_id": line_id,
            "currency": currency,
            # The row's value in its own currency; equal to amount for IDR.
            "original_currency": (amount / rate) if rate else amount,
            "exchange_rate": rate,
            "status": head.get("status"),
            "created_at": head.get("created_at"),
            "created_by": None,
        }

    # The receivable is built from the parts, not from the sheet's own `total`.
    # The two normally agree, but where they do not it is `total` that is wrong,
    # and deriving it here means the entry can never post out of balance.
    subtotal = sum((_dec(r.get("subtotal")) for r in lines), Decimal("0"))
    tax = sum((_dec(r.get("tax")) for r in lines), Decimal("0"))
    total = subtotal + tax

    ar_code, ar_name = coa_by_name[receivable_account(currency)]
    out = [row(ar_code, ar_name, total, Decimal("0"), f"{ref_no}#AR", head)]

    for line in sorted(lines, key=lambda r: str(r.get("service_code") or "")):
        code, name = coa_by_name[str(line.get("account") or "").strip()]
        out.append(row(code, name, Decimal("0"), _dec(line.get("subtotal")),
                       f"{ref_no}#{line.get('service_code')}", line))

    if tax:
        vat_code, vat_name = coa_by_name[VAT_ACCOUNT]
        out.append(row(vat_code, vat_name, Decimal("0"), tax, f"{ref_no}#VAT", head))

    return out


def unmapped_accounts(rows: Iterable[Dict[str, Any]],
                      coa_by_name: Dict[str, Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Rows whose revenue account name is absent from the chart of accounts."""
    return [r for r in rows if str(r.get("account") or "").strip() not in coa_by_name]


def build_journal(rows: Iterable[Dict[str, Any]],
                  coa_by_name: Dict[str, Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Return ``gl`` rows for every invoice in ``rows``, grouped by ``ref_no``."""
    invoices: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        invoices.setdefault(str(r["ref_no"]), []).append(r)

    out: List[Dict[str, Any]] = []
    for ref_no in sorted(invoices):
        out.extend(build_invoice_journal(invoices[ref_no], coa_by_name))
    return out


def journal_is_balanced(gl_rows: Iterable[Dict[str, Any]],
                        tolerance: Decimal = Decimal("0.005")) -> List[str]:
    """Return the ref_nos whose debits and credits do not agree.

    Sub-cent, because the receivable is derived from the same figures as the
    credits: any real difference is a defect, not rounding.
    """
    net: Dict[str, Decimal] = {}
    for r in gl_rows:
        net[r["ref_no"]] = net.get(r["ref_no"], Decimal("0")) + _dec(r["debit"]) - _dec(r["credit"])
    return sorted(ref for ref, value in net.items() if abs(value) > tolerance)
