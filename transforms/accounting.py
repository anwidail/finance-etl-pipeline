import pandas as pd
from datetime import datetime, timezone

# Account IDs — must match data/seed_finance_accounts.sql
ACCOUNT_AR = 1        # 1100 Accounts Receivable (asset)
ACCOUNT_REVENUE = 2   # 4100 Sales Revenue (revenue)
ACCOUNT_TAX = 3       # 2100 Tax Payable - PPN (liability)


def build_daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate transactions by date and produce finance_summary rows.

    For each day, creates 3 rows:
        - Accounts Receivable : debit  = gross amount (customer owes)
        - Sales Revenue       : credit = net amount   (company earns)
        - Tax Payable PPN     : credit = tax amount   (owed to government)
    """
    daily = df.groupby("summary_date").agg(
        sum_gross=("gross_amount", "sum"),
        sum_net=("net_amount", "sum"),
        sum_tax=("tax_amount", "sum"),
        count=("transaction_id", "count"),
    ).reset_index()

    rows = []
    for _, day in daily.iterrows():
        sd = day["summary_date"]
        cnt = int(day["count"])

        rows.append({
            "summary_date": sd,
            "account_id": ACCOUNT_AR,
            "total_debit": round(day["sum_gross"], 2),
            "total_credit": 0,
            "net_amount": round(day["sum_gross"], 2),
            "transaction_count": cnt,
        })

        rows.append({
            "summary_date": sd,
            "account_id": ACCOUNT_REVENUE,
            "total_debit": 0,
            "total_credit": round(day["sum_net"], 2),
            "net_amount": round(day["sum_net"], 2),
            "transaction_count": cnt,
        })

        rows.append({
            "summary_date": sd,
            "account_id": ACCOUNT_TAX,
            "total_debit": 0,
            "total_credit": round(day["sum_tax"], 2),
            "net_amount": round(day["sum_tax"], 2),
            "transaction_count": cnt,
        })

    result = pd.DataFrame(rows)
    result["etl_loaded_at"] = datetime.now(timezone.utc)
    return result
