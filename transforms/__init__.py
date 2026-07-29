import logging

import pandas as pd

from .accounting import build_daily_summary
from .clean import filter_completed
from .receivable_payment import transform_receivable_payment_source_record
from .sales_invoice import transform_sales_invoice_source_record
from .sales_return import transform_sales_return_source_record
from .tax import calculate_ppn

log = logging.getLogger(__name__)


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transform raw transactions into finance_summary rows.

    Pipeline:
        filter_completed  →  calculate_ppn  →  build_daily_summary
    """
    df = filter_completed(df)

    if df.empty:
        log.info("TRANSFORM: no completed transactions to process")
        return pd.DataFrame()

    df = calculate_ppn(df)
    result = build_daily_summary(df)

    log.info(f"TRANSFORM: {len(df)} transactions → {len(result)} summary rows")
    return result


__all__ = [
    "transform",
    "transform_sales_invoice_source_record",
    "transform_receivable_payment_source_record",
    "transform_sales_return_source_record",
]
