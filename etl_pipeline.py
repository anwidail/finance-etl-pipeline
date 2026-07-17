"""Main ETL orchestrator for callback-based finance modules."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from load.activity_log import build_activity_row, upsert_activity_rows
from load.etl_state import get_watermark, set_watermark
from load.gl_reporting import enrich_gl_reporting_engine
from load.sales_module_loader import process_records_by_module
from transforms import (
    transform_cash_in_source_record,
    transform_cash_out_source_record,
    transform_manual_journal_source_record,
    transform_payable_payment_source_record,
    transform_purchase_invoice_source_record,
    transform_purchase_return_source_record,
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
    "manual_journal": transform_manual_journal_source_record,
    "sales_invoice": transform_sales_invoice_source_record,
    "receivable_payment": transform_receivable_payment_source_record,
    "sales_return": transform_sales_return_source_record,
    "purchase_invoice": transform_purchase_invoice_source_record,
    "payable_payment": transform_payable_payment_source_record,
    "purchase_return": transform_purchase_return_source_record,
    "cash_in": transform_cash_in_source_record,
    "cash_out": transform_cash_out_source_record,
}

ENDPOINT_MAP = {
    "manual_journal": "manual_journals",
    "sales_invoice": "sales_invoices",
    "receivable_payment": "receivable_payments",
    "sales_return": "sales_returns",
    "purchase_invoice": "purchases_invoices",
    "payable_payment": "payable_payments",
    "purchase_return": "purchases_returns",
    "cash_in": "cash_ins",
    "cash_out": "cash_outs",
}

# Reverse lookup: source endpoint -> module name (for bucketing the delta).
ENDPOINT_TO_MODULE = {endpoint: module for module, endpoint in ENDPOINT_MAP.items()}

# --- Incremental extraction (watermark) config ---
# ETL_INCREMENTAL=1 (default): pull only callbacks newer than the stored
# watermark. Set to 0 to fall back to the legacy "N newest per module" behavior.
INCREMENTAL = os.getenv("ETL_INCREMENTAL", "1") == "1"
# Re-scan a small tail before the watermark on each run so rows committed
# slightly out of timestamp order are not skipped. Idempotent upserts make the
# overlap harmless.
WATERMARK_OVERLAP_MINUTES = int(os.getenv("ETL_WATERMARK_OVERLAP_MINUTES", "5"))
# First run only (no watermark yet): how far back to seed the initial window.
# Going forward the watermark guarantees completeness; older untouched history
# is not needed because any later edit arrives as a fresh callback. Set
# ETL_INITIAL_SINCE=YYYY-MM-DD (or an old date) for an explicit / full backfill.
INITIAL_SINCE = os.getenv("ETL_INITIAL_SINCE")
INITIAL_DAYS = int(os.getenv("ETL_INITIAL_DAYS", "45"))


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
            method,
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
            data = payload.get("data") or {}
            rec["source_id"] = data.get("id")
            rec["status"] = data.get("status")
            # The action lives in the body's `method` (POST/PUT/PATCH/DELETE); the
            # DB `method` column is only the HTTP verb of the callback delivery
            # (always POST) and never carries DELETE, so it cannot be used here.
            rec["method"] = payload.get("method")
            records.append(rec)

        if len(records) >= limit:
            break

    return records


def _to_naive_datetime(value):
    """Normalize a pandas/py datetime to a naive python datetime (or None)."""
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts is pd.NaT:
        return None
    ts = ts.to_pydatetime()
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def extract_source_since(source_engine, since, modules: list[str], until=None):
    """Pull non-deleted callbacks in a created_at window and bucket per module.

    Window is (since, until]: created_at > `since` (or all history when None) and
    created_at <= `until` (or up to latest when None), in one pass, grouped by
    target module via the endpoint. Returns (buckets, max_created_at) where
    max_created_at is the frontier across ALL scanned rows (not just matched
    modules) — used as the new watermark for continuous runs.
    """
    where = ["body IS NOT NULL", "deleted_at IS NULL"]
    params: dict = {}
    if since is not None:
        where.append("created_at > :since")
        params["since"] = since
    if until is not None:
        where.append("created_at <= :until")
        params["until"] = until

    sql = text(
        f"""
        SELECT
            id AS callback_id,
            method,
            body,
            created_at
        FROM {SOURCE_CALLBACK_TABLE}
        WHERE {" AND ".join(where)}
        ORDER BY created_at ASC
        """
    )

    df = pd.read_sql(sql, source_engine, params=params)

    buckets: dict[str, list[dict]] = {module: [] for module in modules}
    max_created_at = None

    for rec in df.to_dict(orient="records"):
        created = _to_naive_datetime(rec.get("created_at"))
        if created is not None and (max_created_at is None or created > max_created_at):
            max_created_at = created

        payload = parse_callback_body(rec.get("body"))
        if not payload:
            continue

        module = ENDPOINT_TO_MODULE.get(payload.get("end_point"))
        if module in buckets:
            # Attach the document id (data.id UUID), status and action so the
            # loader can apply DELETE / draft / edit semantics keyed strictly on
            # source_id. The action is the body's `method` (POST/PUT/PATCH/DELETE);
            # the DB `method` column is only the callback's HTTP verb (always POST)
            # and never carries DELETE, so it cannot be used to detect deletions.
            data = payload.get("data") or {}
            rec["source_id"] = data.get("id")
            rec["status"] = data.get("status")
            rec["method"] = payload.get("method")
            buckets[module].append(rec)

    return buckets, max_created_at


def _resolve_initial_since():
    """Lower bound for the very first run (no watermark yet)."""
    if INITIAL_SINCE:
        # Explicit override, e.g. "2026-01-01" or "1970-01-01" for a full backfill.
        return datetime.fromisoformat(INITIAL_SINCE)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return now_naive - timedelta(days=INITIAL_DAYS)


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
    module_names = os.getenv(
        "ETL_MODULES",
        "manual_journal,sales_invoice,receivable_payment,sales_return,purchase_invoice,payable_payment,purchase_return,cash_in,cash_out",
    )
    requested = [m.strip() for m in module_names.split(",") if m.strip()]
    valid_modules = [m for m in requested if m in MODULE_TRANSFORMERS]

    unknown_modules = sorted(set(requested) - set(valid_modules))
    if unknown_modules:
        log.warning("Unknown ETL modules ignored: %s", ", ".join(unknown_modules))

    return valid_modules


def run_module_etl(finance_session_factory, module_name: str, source_records: list[dict]):
    """Run ETL flow for one finance module from pre-extracted source records."""
    transform_func = MODULE_TRANSFORMERS[module_name]

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
        "%s: success=%s failed=%s rows=%s deleted=%s",
        module_name,
        result.get("success_records", 0),
        result.get("failed_records", 0),
        result.get("processed_rows", 0),
        result.get("deleted_documents", 0),
    )

    if result.get("failed_detail"):
        sample = result["failed_detail"][:5]
        log.warning("%s: sample failed detail: %s", module_name, sample)

    return result


# ============================================================
# MAIN PIPELINE
# ============================================================


def run_pipeline(since=None, until=None, update_watermark=None):
    """Run callback-based ETL for selected finance modules.

    Modes:
      * default (since=until=None): continuous incremental from the stored
        watermark; advances the watermark on success.
      * ad-hoc range (since and/or until given): process the created_at window
        (since, until] regardless of the watermark, and by default DO NOT advance
        it — so a period backfill never makes the daily run skip newer data.

    `update_watermark` overrides the default (True/False) if you really want an
    ad-hoc range to also move the watermark.
    """
    modules = _parse_modules_from_env()
    if not modules:
        log.warning("No valid modules configured. Nothing to run.")
        return

    ad_hoc = since is not None or until is not None
    if update_watermark is None:
        update_watermark = INCREMENTAL and not ad_hoc

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
    new_watermark = None

    try:
        # ---- Extract ----
        if ad_hoc:
            log.info("Ad-hoc range extract: since=%s until=%s (update_watermark=%s)", since, until, update_watermark)
            buckets, new_watermark = extract_source_since(source_engine, since, modules, until=until)
            log.info("Extracted range: %s callbacks matched across modules", sum(len(v) for v in buckets.values()))
        elif INCREMENTAL:
            watermark = get_watermark(finance_engine)
            if watermark is not None:
                since = watermark - timedelta(minutes=WATERMARK_OVERLAP_MINUTES)
                log.info("Incremental extract since %s (watermark %s, overlap %sm)", since, watermark, WATERMARK_OVERLAP_MINUTES)
            else:
                since = _resolve_initial_since()
                log.info("No watermark yet — first run initial window since %s", since)

            buckets, new_watermark = extract_source_since(source_engine, since, modules)
            log.info("Extracted delta: %s callbacks matched across modules", sum(len(v) for v in buckets.values()))
        else:
            log.info("Incremental disabled — legacy 'newest %s per module' extraction", LIMIT_PER_MODULE)
            buckets = {
                module_name: extract_source_records(source_engine, module_name=module_name, limit=LIMIT_PER_MODULE)
                for module_name in modules
            }

        # ---- Activity log ----
        # Record every extracted callback as a user activity (create/update/
        # delete), independent of the ledger's approved-only / latest-only
        # filtering. Idempotent on callback_id, so re-runs never duplicate.
        activity_rows = []
        for module_name, recs in buckets.items():
            for rec in recs:
                payload = parse_callback_body(rec.get("body"))
                if not payload:
                    continue
                activity_rows.append(build_activity_row(rec, payload, module_name))
        if activity_rows:
            with finance_session_factory() as session:
                logged = upsert_activity_rows(session, activity_rows, chunk_size=CHUNK_SIZE)
                session.commit()
            log.info("Activity log: %s activities upserted", logged)

        # ---- Transform + Load per module ----
        for module_name in modules:
            result = run_module_etl(
                finance_session_factory=finance_session_factory,
                module_name=module_name,
                source_records=buckets.get(module_name, []),
            )
            module_summaries.append((module_name, result))
            total_records += result.get("input_records", 0)
            total_failed += result.get("failed_records", 0)
            total_rows += result.get("processed_rows", 0)

        # Enrich the unified ledger with reporting data from the chart of
        # accounts (coa). Runs once after all modules are loaded so every gl row
        # — including this run's fresh inserts — gets its reporting/coa_code.
        enriched = enrich_gl_reporting_engine(finance_engine)
        log.info("GL reporting enriched from coa: %s rows updated", enriched)

        # Advance the watermark only after a fully successful run, to the max
        # created_at actually seen (never to "now" — a callback could arrive
        # mid-run with an earlier timestamp and must not be skipped). Ad-hoc
        # range runs skip this by default so they don't disturb daily runs.
        if update_watermark and new_watermark is not None:
            set_watermark(finance_engine, new_watermark)
            log.info("Watermark advanced to %s", new_watermark)

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


def _parse_bound(value: str, end_of_day: bool = False):
    """Parse a CLI date/datetime. Date-only `until` snaps to end of day."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if end_of_day and len(value.strip()) == 10:  # "YYYY-MM-DD" with no time
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Finance callback ETL. No args = continuous incremental run.",
    )
    parser.add_argument("--since", help="Start of created_at window (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'), exclusive")
    parser.add_argument("--until", help="End of created_at window (YYYY-MM-DD or datetime), inclusive")
    parser.add_argument("--last-days", type=int, help="Shortcut: process the last N days (e.g. 30 for last month)")
    parser.add_argument(
        "--set-watermark",
        action="store_true",
        help="Also advance the watermark for an ad-hoc range run (off by default)",
    )
    args = parser.parse_args()

    since = _parse_bound(args.since)
    until = _parse_bound(args.until, end_of_day=True)
    if args.last_days is not None:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.last_days)

    update_watermark = True if args.set_watermark else None
    run_pipeline(since=since, until=until, update_watermark=update_watermark)
