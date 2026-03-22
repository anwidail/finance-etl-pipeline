import logging

import pandas as pd

from transforms.clean import filter_completed
from transforms.tax import calculate_ppn
from transforms.accounting import build_daily_summary

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
