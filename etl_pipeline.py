"""Main ETL orchestrator for callback-based sales modules."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from load.sales_module_loader import process_records_by_module
from transforms import (
    transform_receivable_payment_source_record,
    transform_sales_invoice_source_record,
    transform_sales_return_source_record,
)

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

SOURCE_DB = {
    "host": os.getenv("SOURCE_DB_HOST", "localhost"),
    "port": int(os.getenv("SOURCE_DB_PORT", "3306")),
    "user": os.getenv("SOURCE_DB_USER", "your_user"),
    "password": os.getenv("SOURCE_DB_PASSWORD", "your_password"),
    "database": os.getenv("SOURCE_DB_NAME", "database_a"),
}

FINANCE_DB = {
    "host": os.getenv("FINANCE_DB_HOST", "localhost"),
    "port": int(os.getenv("FINANCE_DB_PORT", "3306")),
    "user": os.getenv("FINANCE_DB_USER", "your_user"),
    "password": os.getenv("FINANCE_DB_PASSWORD", "your_password"),
    "database": os.getenv("FINANCE_DB_NAME", "finance_db"),
}

SOURCE_DB_URL = os.getenv("SOURCE_DB_URL")
FINANCE_DB_URL = os.getenv("FINANCE_DB_URL")
SOURCE_CALLBACK_TABLE = os.getenv("SOURCE_CALLBACK_TABLE", "callback_zahir")
LIMIT_PER_MODULE = int(os.getenv("ETL_LIMIT_PER_MODULE", "200"))
CHUNK_SIZE = int(os.getenv("ETL_CHUNK_SIZE", "500"))

MODULE_TRANSFORMERS = {
    "sales_invoice": transform_sales_invoice_source_record,
    "receivable_payment": transform_receivable_payment_source_record,
    "sales_return": transform_sales_return_source_record,
}

ENDPOINT_MAP = {
    "sales_invoice": "sales_invoices",
    "receivable_payment": "receivable_payments",
    "sales_return": "sales_returns",
}


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
    url = (
        f"mysql+mysqlconnector://{config['user']}:{config['password']}"
        f"@{config['host']}:{config['port']}/{config['database']}"
    )
    return create_engine(url, future=True)


def get_source_engine():
    """Build source DB engine using SOURCE_DB_URL if provided."""
    if SOURCE_DB_URL:
        return create_engine(SOURCE_DB_URL, future=True)
    return get_engine(SOURCE_DB)


def get_finance_engine():
    """Build finance DB engine using FINANCE_DB_URL if provided."""
    if FINANCE_DB_URL:
        return create_engine(FINANCE_DB_URL, future=True)
    return get_engine(FINANCE_DB)


def parse_callback_body(raw_body):
    """Parse callback body safely, including double-encoded JSON."""
    if raw_body is None:
        return None

    if isinstance(raw_body, dict):
        return raw_body

    if not isinstance(raw_body, str):
        return None

    try:
        payload = json.loads(raw_body)
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def extract_source_records(source_engine, module_name: str, limit: int) -> list[dict]:
    """Extract raw callback records and keep only target module endpoint."""
    endpoint = ENDPOINT_MAP[module_name]

    sql = text(
        f"""
        SELECT
            id AS callback_id,
            body,
            created_at
        FROM {SOURCE_CALLBACK_TABLE}
        WHERE body IS NOT NULL
        ORDER BY created_at DESC
        LIMIT :limit_n
        """
    )

    fetch_limit = max(limit * 30, 300)
    df = pd.read_sql(sql, source_engine, params={"limit_n": fetch_limit})

    records = []
    for rec in df.to_dict(orient="records"):
        payload = parse_callback_body(rec.get("body"))
        if not payload:
            continue

        if payload.get("end_point") == endpoint:
            records.append(rec)

        if len(records) >= limit:
            break

    return records


# ============================================================
# LEGACY LOAD HELPERS (kept for compatibility with tests)
# ============================================================


def load(df: pd.DataFrame, engine, table_name: str, mode: str = "append"):
    """Insert DataFrame into target table."""
    chunk_size = 5000

    df.to_sql(
        name=table_name,
        con=engine,
        if_exists=mode,
        index=False,
        chunksize=chunk_size,
        method="multi",
    )

    log.info("LOAD: %s rows -> %s (mode=%s)", len(df), table_name, mode)


def load_with_upsert(df: pd.DataFrame, engine, table_name: str, unique_keys: list[str]):
    """Legacy generic upsert utility."""
    staging = f"_staging_{table_name}"
    df.to_sql(staging, engine, if_exists="replace", index=False, method="multi")

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

    log.info("UPSERT: %s rows -> %s", len(df), table_name)


def _parse_modules_from_env() -> list[str]:
    module_names = os.getenv("ETL_MODULES", "sales_invoice,receivable_payment,sales_return")
    requested = [m.strip() for m in module_names.split(",") if m.strip()]
    valid_modules = [m for m in requested if m in MODULE_TRANSFORMERS]

    unknown_modules = sorted(set(requested) - set(valid_modules))
    if unknown_modules:
        log.warning("Unknown ETL modules ignored: %s", ", ".join(unknown_modules))

    return valid_modules


def run_module_etl(source_engine, finance_session_factory, module_name: str, limit: int):
    """Run ETL flow for one sales module."""
    transform_func = MODULE_TRANSFORMERS[module_name]
    source_records = extract_source_records(source_engine, module_name=module_name, limit=limit)

    if not source_records:
        log.info("%s: no source records found", module_name)
        return {
            "input_records": 0,
            "success_records": 0,
            "failed_records": 0,
            "processed_rows": 0,
        }

    log.info("%s: extracted %s source records", module_name, len(source_records))

    with finance_session_factory() as session:
        result = process_records_by_module(
            session=session,
            module_name=module_name,
            source_records=source_records,
            transform_func=transform_func,
            chunk_size=CHUNK_SIZE,
        )
        session.commit()

    log.info(
        "%s: success=%s failed=%s rows=%s",
        module_name,
        result.get("success_records", 0),
        result.get("failed_records", 0),
        result.get("processed_rows", 0),
    )

    if result.get("failed_detail"):
        sample = result["failed_detail"][:5]
        log.warning("%s: sample failed detail: %s", module_name, sample)

    return result


# ============================================================
# MAIN PIPELINE
# ============================================================


def run_pipeline():
    """Run callback-based ETL for selected finance modules."""
    modules = _parse_modules_from_env()
    if not modules:
        log.warning("No valid modules configured. Nothing to run.")
        return

    source_engine = get_source_engine()
    finance_engine = get_finance_engine()
    finance_session_factory = sessionmaker(bind=finance_engine, autoflush=False, autocommit=False, future=True)

    log.info("=" * 60)
    log.info("ETL Pipeline started (modules: %s)", ", ".join(modules))
    log.info("=" * 60)

    total_records = 0
    total_failed = 0
    total_rows = 0
    module_summaries = []

    try:
        for module_name in modules:
            result = run_module_etl(
                source_engine=source_engine,
                finance_session_factory=finance_session_factory,
                module_name=module_name,
                limit=LIMIT_PER_MODULE,
            )
            module_summaries.append((module_name, result))
            total_records += result.get("input_records", 0)
            total_failed += result.get("failed_records", 0)
            total_rows += result.get("processed_rows", 0)

        for module_name, result in module_summaries:
            log.info(
                "Module summary [%s]: input=%s success=%s failed=%s rows=%s",
                module_name,
                result.get("input_records", 0),
                result.get("success_records", 0),
                result.get("failed_records", 0),
                result.get("processed_rows", 0),
            )

        log.info(
            "Pipeline finished: input_records=%s failed_records=%s processed_rows=%s",
            total_records,
            total_failed,
            total_rows,
        )

    except Exception as e:
        log.error("Pipeline failed: %s", e, exc_info=True)
        raise

    finally:
        source_engine.dispose()
        finance_engine.dispose()


if __name__ == "__main__":
    run_pipeline()
