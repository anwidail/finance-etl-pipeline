"""
ETL Pipeline: Database A → Process → Finance Database

Flow:
    1. EXTRACT  — Read raw data from source database (Database A)
    2. TRANSFORM — Process, clean, calculate
    3. LOAD — Insert results into finance database

Dependencies:
    pip install mysql-connector-python pandas sqlalchemy

Usage:
    python etl_pipeline.py
"""

import os
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime, timezone
from dotenv import load_dotenv
import logging

from transforms import transform

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

# Source: Database A
SOURCE_DB = {
    "host": os.getenv("SOURCE_DB_HOST", "localhost"),
    "port": int(os.getenv("SOURCE_DB_PORT", "3306")),
    "user": os.getenv("SOURCE_DB_USER", "your_user"),
    "password": os.getenv("SOURCE_DB_PASSWORD", "your_password"),
    "database": os.getenv("SOURCE_DB_NAME", "database_a"),
}

# Destination: Finance Database
FINANCE_DB = {
    "host": os.getenv("FINANCE_DB_HOST", "localhost"),
    "port": int(os.getenv("FINANCE_DB_PORT", "3306")),
    "user": os.getenv("FINANCE_DB_USER", "your_user"),
    "password": os.getenv("FINANCE_DB_PASSWORD", "your_password"),
    "database": os.getenv("FINANCE_DB_NAME", "finance_db"),
}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"etl_{datetime.now(timezone.utc):%Y%m%d}.log"),
    ],
)
log = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def get_engine(config: dict):
    """Create SQLAlchemy engine from config dict."""
    url = f"mysql+mysqlconnector://{config['user']}:{config['password']}@{config['host']}:{config['port']}/{config['database']}"
    return create_engine(url)


# ============================================================
# 1. EXTRACT — Read from Database A
# ============================================================

def extract(engine) -> pd.DataFrame:
    """
    Pull transactions joined with product and customer info from database_a.
    Only grabs the last 7 days to keep incremental loads fast.
    """
    query = """
        SELECT
            t.id            AS transaction_id,
            t.customer_id,
            c.name          AS customer_name,
            t.product_id,
            p.name          AS product_name,
            p.category      AS product_category,
            t.quantity,
            t.unit_price,
            t.total_amount,
            t.status,
            t.created_at
        FROM transactions t
        JOIN customers c ON c.id = t.customer_id
        JOIN products  p ON p.id = t.product_id
        WHERE t.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    """

    df = pd.read_sql(query, engine)
    log.info(f"EXTRACT: {len(df)} rows from source DB")
    return df


# ============================================================
# 3. LOAD — Insert into Finance Database
# ============================================================

def load(df: pd.DataFrame, engine, table_name: str, mode: str = "append"):
    """
    Insert processed data into finance database.

    Args:
        mode:
            "append"  — Add new rows (default, safe for incremental loads)
            "replace" — Drop table & recreate (full refresh, use with caution)

    For large datasets, chunk the insert to avoid memory/timeout issues.
    """
    chunk_size = 5000

    df.to_sql(
        name=table_name,
        con=engine,
        if_exists=mode,       # "append" or "replace"
        index=False,
        chunksize=chunk_size,
        method="multi",       # faster bulk insert
    )

    log.info(f"LOAD: {len(df)} rows → finance_db.{table_name} (mode={mode})")


def load_with_upsert(df: pd.DataFrame, engine, table_name: str, unique_keys: list[str]):
    """
    Upsert (insert or update if exists).
    Use this when you might re-process the same data and want to avoid duplicates.

    Requires a UNIQUE INDEX on the unique_keys columns in the target table.
    See data/seed_finance_accounts.sql for the index creation.

    Args:
        unique_keys: List of column names that form the unique constraint,
                     e.g. ["summary_date", "account_id"]
    """
    # Step 1: Load into a temp staging table
    staging = f"_staging_{table_name}"
    df.to_sql(staging, engine, if_exists="replace", index=False, method="multi")

    # Step 2: Upsert from staging → target
    columns = ", ".join(df.columns)
    update_cols = [col for col in df.columns if col not in unique_keys]
    updates = ", ".join([f"{col}=VALUES({col})" for col in update_cols])

    upsert_sql = f"""
        INSERT INTO {table_name} ({columns})
        SELECT {columns} FROM {staging}
        ON DUPLICATE KEY UPDATE {updates}
    """

    with engine.begin() as conn:
        conn.execute(text(upsert_sql))
        conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))

    log.info(f"UPSERT: {len(df)} rows → finance_db.{table_name}")


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_pipeline():
    log.info("=" * 50)
    log.info("ETL Pipeline started")
    log.info("=" * 50)

    source_engine = get_engine(SOURCE_DB)
    finance_engine = get_engine(FINANCE_DB)

    try:
        # 1. Extract
        df = extract(source_engine)

        if df.empty:
            log.info("No data to process. Exiting.")
            return

        # 2. Transform
        df = transform(df)

        if df.empty:
            log.info("No completed transactions to load. Exiting.")
            return

        # 3. Load (upsert so re-runs update instead of duplicating)
        load_with_upsert(
            df, finance_engine,
            table_name="finance_summary",
            unique_keys=["summary_date", "account_id"],
        )

        log.info("✅ Pipeline completed successfully")

    except Exception as e:
        log.error(f"❌ Pipeline failed: {e}", exc_info=True)
        raise

    finally:
        source_engine.dispose()
        finance_engine.dispose()


if __name__ == "__main__":
    run_pipeline()
