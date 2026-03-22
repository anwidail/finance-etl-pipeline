# ETL Pipeline

Extracts transaction data from a source MySQL database (`database_a`), transforms it (calculates PPN 11% tax, aggregates daily summaries), and loads the results into a finance database using upsert.

## Prerequisites

- Python 3.10+
- MySQL 8.0+ running locally (or accessible remotely)
- A MySQL user with permission to create/modify tables in both databases

## Quick Start

```bash
# 1. Clone and install
git clone <repo-url>
cd husband-project
make install

# 2. Configure credentials
cp .env.example .env
# Edit .env with your actual MySQL credentials

# 3. Create the databases
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS database_a;"
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS finance_db;"

# 4. Create the MySQL user (optional, if you don't have one)
mysql -u root -p -e "
  CREATE USER IF NOT EXISTS 'etl_user'@'localhost' IDENTIFIED BY 'etl_password';
  GRANT ALL PRIVILEGES ON database_a.* TO 'etl_user'@'localhost';
  GRANT ALL PRIVILEGES ON finance_db.* TO 'etl_user'@'localhost';
  FLUSH PRIVILEGES;
"

# 5. Run migrations (creates all tables)
make migrate

# 6. Seed mock data
mysql -u etl_user -p database_a < data/seed_database_a.sql
mysql -u etl_user -p finance_db < data/seed_finance_accounts.sql

# 7. Run the pipeline
make run
```

## What the Pipeline Does

```
database_a                          finance_db
┌──────────────┐                    ┌──────────────────┐
│ customers    │                    │ finance_summary   │
│ products     │──► EXTRACT ──►     │                  │
│ transactions │    TRANSFORM ──►   │  - summary_date  │
└──────────────┘    LOAD (upsert)──►│  - account_id    │
                                    │  - total_debit   │
                                    │  - total_credit  │
                                    │  - net_amount    │
                                    │  - txn_count     │
                                    └──────────────────┘
```

### Extract
Pulls the last 7 days of completed transactions from `database_a`, joined with customer and product info.

### Transform
1. Filters only `completed` transactions (skips `pending` and `cancelled`)
2. Calculates **PPN 11%** tax on each transaction
3. Aggregates by date and produces **3 summary rows per day**:
   - **Accounts Receivable (1100)** — debit = gross amount (price + tax)
   - **Sales Revenue (4100)** — credit = net amount (price before tax)
   - **Tax Payable PPN (2100)** — credit = tax amount

### Load (Upsert)
Uses `INSERT ... ON DUPLICATE KEY UPDATE` via a staging table. Re-running the pipeline on the same date range **updates** existing rows instead of creating duplicates. This requires the unique index on `(summary_date, account_id)` created by the finance seed script.

## Configuration

All database connection settings are stored in `.env`. See `.env.example` for the required variables:

```env
# Source: Database A
SOURCE_DB_HOST=localhost
SOURCE_DB_PORT=3306
SOURCE_DB_USER=your_user
SOURCE_DB_PASSWORD=your_password
SOURCE_DB_NAME=database_a

# Destination: Finance Database
FINANCE_DB_HOST=localhost
FINANCE_DB_PORT=3306
FINANCE_DB_USER=your_user
FINANCE_DB_PASSWORD=your_password
FINANCE_DB_NAME=finance_db
```

## Timezone

All timestamps (ETL metadata, model defaults, log filenames) use **UTC** via `datetime.now(timezone.utc)`. Indonesia spans 3 timezones (WIB/WITA/WIT), so UTC keeps stored data consistent and timezone-neutral. Convert to local time only at the presentation layer.

## Usage

```bash
# Run the ETL pipeline
make run

# Or manually
source venv/bin/activate
python etl_pipeline.py
```

## Mock Data

The `data/` directory contains SQL seed files to populate both databases with sample data for testing.

| File | Target DB | Contents |
|------|-----------|----------|
| `seed_database_a.sql` | `database_a` | 10 customers, 8 products, 28 transactions (mix of completed/pending/cancelled) |
| `seed_finance_accounts.sql` | `finance_db` | 4 chart-of-accounts entries + unique index for upsert |

To re-seed (reset mock data):
```bash
mysql -u etl_user -p database_a -e "DELETE FROM transactions; DELETE FROM products; DELETE FROM customers;"
mysql -u etl_user -p database_a < data/seed_database_a.sql

mysql -u etl_user -p finance_db -e "DELETE FROM finance_summary;"
mysql -u etl_user -p finance_db < data/seed_finance_accounts.sql
```

## Database Migrations

Migrations are managed with [Alembic](https://alembic.sqlalchemy.org/). There are two separate migration tracks — one for each database.

```bash
# Apply all migrations to both databases
make migrate

# Apply to a single database
make migrate-source
make migrate-finance

# Rollback the last migration
make rollback-source
make rollback-finance

# Auto-generate a new migration after editing models
make revision-source msg="add index to transactions"
make revision-finance msg="add currency column"
```

### How to modify the schema

1. **Edit the model** in `models/source.py` or `models/finance.py`
2. **Generate the migration**: `make revision-source msg="describe change"`
3. **Apply the migration**: `make migrate-source`

### Schema overview

**Source DB** (`database_a`) — operational data:
- `customers` — customer records
- `products` — product catalog (Electronics, Food & Beverage, Office Supplies, Clothing)
- `transactions` — raw transaction log with status (pending/completed/cancelled)

**Finance DB** (`finance_db`) — processed financial data:
- `accounts` — chart of accounts (AR, Revenue, Tax Payable, COGS)
- `journal_entries` — accounting events (header)
- `journal_lines` — debit/credit lines per entry
- `finance_summary` — daily aggregated summary (ETL target table)

## Project Structure

```
.
├── etl_pipeline.py          # Main ETL script (extract, load, orchestrator)
├── transforms/              # Transform logic (clean → tax → accounting)
│   ├── __init__.py          # transform() entry point
│   ├── clean.py             # filter_completed() — filter + type prep
│   ├── tax.py               # calculate_ppn() — PPN 11% calculation
│   └── accounting.py        # build_daily_summary() — aggregate + map to accounts
├── models/
│   ├── source.py            # Source DB models (SQLAlchemy)
│   └── finance.py           # Finance DB models (SQLAlchemy)
├── data/
│   ├── seed_database_a.sql  # Mock data for source DB
│   └── seed_finance_accounts.sql  # Chart of accounts + unique index
├── alembic/
│   ├── source/              # Migrations for source DB
│   └── finance/             # Migrations for finance DB
├── tests/
│   ├── conftest.py          # Shared pytest fixtures
│   ├── test_etl_pipeline.py # Tests for transform + load
│   └── test_models.py       # Tests for SQLAlchemy models
├── requirements.txt
├── Makefile
├── .env                     # Your credentials (git-ignored)
└── .env.example             # Template
```
