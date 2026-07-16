from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Tuple


# ============================================================
# REALIZED FOREX
# ============================================================
# On a foreign-currency settlement the invoice was booked at one rate and the
# cash settles at another; the difference is a realized forex gain/loss. The
# source often carries that difference on the Bank Administrative account
# (6924), so on FX documents we reclassify it to the realized forex accounts
# (normalized gl codes). A debit is a loss, a credit is a gain.
REALIZE_FOREX_GAIN_CODE = "8104"
REALIZE_FOREX_GAIN_NAME = "Realize Forex Gain"
REALIZE_FOREX_LOSS_CODE = "9104"
REALIZE_FOREX_LOSS_NAME = "Realize Forex Loss"

# Bank fee the bank deducts on a settlement. On non-FX (IDR) documents the
# balancing difference stays here; on FX documents it is a forex diff instead.
BANK_CHARGE_CODE = "6924"
BANK_CHARGE_NAME = "Bank Administrative"


def _to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def is_fx_settlement(cash: Dict[str, Any]) -> bool:
    """True when the payment settles in a foreign currency (rate != 1).

    Detected from the cash side: a non-IDR currency code or an exchange rate
    that is neither 1 (base currency) nor 0/absent.
    """
    cash = cash or {}
    currency = ((cash.get("currency") or {}).get("code") or "").upper()
    rate = _to_decimal(cash.get("exchange_rate"))
    return currency not in ("", "IDR") or rate not in (Decimal("0"), Decimal("1"))


def forex_account(is_debit: bool) -> Tuple[str, str]:
    """Resolve (account_code, coa_name) for a realized forex difference.

    A debit posting is a realized loss; a credit posting is a realized gain.
    """
    if is_debit:
        return REALIZE_FOREX_LOSS_CODE, REALIZE_FOREX_LOSS_NAME
    return REALIZE_FOREX_GAIN_CODE, REALIZE_FOREX_GAIN_NAME
