import pandas as pd


def filter_completed(df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only completed transactions and prepare columns for downstream steps.

    - Filters out pending/cancelled rows
    - Casts total_amount to float (from Decimal)
    - Extracts summary_date from created_at timestamp
    """
    df = df[df["status"] == "completed"].copy()
    df["total_amount"] = df["total_amount"].astype(float)
    df["summary_date"] = pd.to_datetime(df["created_at"]).dt.date
    return df
