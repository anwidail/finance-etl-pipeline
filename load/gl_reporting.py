"""Enrich the unified gl ledger with reporting data from accounting.coa.

gl stores a *simplified* account code that Zahir emits on each line (e.g.
"111211", "1112401"), while the chart of accounts (coa) stores the canonical
dashed form ("1112-11-000", "1112-40-100"). The two reconcile once you drop the
dashes and trim trailing zeros from both sides:

    coa 1112-11-000  ->  111211
    gl  111211       ->  111211      (match)

    coa 1112-40-100  ->  1112401
    gl  1112401      ->  1112401     (match)

This single normalization rule (kept in one place, `NORM_SQL` below) matches the
vast majority of codes deterministically. Codes with no coa counterpart (a few
synthetic helper accounts such as discount/other-tax) are left NULL.
"""
from __future__ import annotations

import re

from sqlalchemy import text


def normalize_account_code(code) -> str | None:
    """Canonical join key: strip non-digits (dashes), then trailing zeros.

    Mirrors NORM_SQL exactly so Python and SQL agree.
    """
    if code is None:
        return None
    digits = re.sub(r"\D", "", str(code))
    digits = digits.rstrip("0")
    return digits or None


# SQL form of normalize_account_code(). gl codes carry no dashes, coa codes do;
# REPLACE handles both, TRIM(TRAILING '0') matches rstrip("0").
NORM_SQL = "TRIM(TRAILING '0' FROM REPLACE({col}, '-', ''))"

# Refresh reporting/coa_code for every gl row by joining coa on the normalized
# code. Idempotent: safe to re-run after each load; unmatched rows reset to NULL.
_ENRICH_SQL = text(
    f"""
    UPDATE gl
    LEFT JOIN coa
        ON {NORM_SQL.format(col='gl.account_code')} = {NORM_SQL.format(col='coa.account_code')}
    SET gl.reporting = coa.reporting,
        gl.coa_code  = coa.account_code
    WHERE gl.account_code IS NOT NULL AND gl.account_code <> ''
    """
)


def enrich_gl_reporting(conn) -> int:
    """Populate gl.reporting and gl.coa_code from coa. Returns rows affected.

    Accepts a SQLAlchemy Engine or Connection.
    """
    if hasattr(conn, "begin") and not hasattr(conn, "execute"):
        # It's an Engine used as context manager elsewhere; open a transaction.
        with conn.begin() as c:  # pragma: no cover - convenience path
            return c.execute(_ENRICH_SQL).rowcount

    result = conn.execute(_ENRICH_SQL)
    return result.rowcount


def enrich_gl_reporting_engine(engine) -> int:
    """Run the enrichment in its own transaction against an Engine."""
    with engine.begin() as conn:
        return conn.execute(_ENRICH_SQL).rowcount


if __name__ == "__main__":
    # Standalone backfill: python -m load.gl_reporting
    import os

    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    load_dotenv()
    url = os.getenv("FINANCE_DB_URL") or (
        f"mysql+pymysql://{os.getenv('FINANCE_DB_USER')}:{os.getenv('FINANCE_DB_PASSWORD')}"
        f"@{os.getenv('FINANCE_DB_HOST', 'localhost')}:{os.getenv('FINANCE_DB_PORT', '3306')}"
        f"/{os.getenv('FINANCE_DB_NAME')}"
    )
    eng = create_engine(url, future=True)
    affected = enrich_gl_reporting_engine(eng)
    print(f"gl reporting enriched: {affected} rows updated")
