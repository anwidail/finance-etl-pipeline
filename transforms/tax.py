import pandas as pd

PPN_RATE = 0.11  # Indonesian VAT (Pajak Pertambahan Nilai)


def calculate_ppn(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add PPN 11% tax columns to each transaction row.

    New columns:
        tax_amount  — PPN on total_amount
        net_amount  — total_amount minus tax
        gross_amount — total_amount plus tax (what customer pays)
    """
    df = df.copy()
    df["tax_amount"] = (df["total_amount"] * PPN_RATE).round(2)
    df["net_amount"] = (df["total_amount"] - df["tax_amount"]).round(2)
    df["gross_amount"] = (df["total_amount"] + df["tax_amount"]).round(2)
    return df
