# 📊 Finance ETL Pipeline (Zahir Callback-Based)

This project implements an **ETL pipeline** that processes raw API callback data from Zahir into structured finance transaction tables, forming the foundation for automated accounting and financial reporting.

The pipeline extracts raw callback data from a source MySQL database (`database_a`), parses and standardizes the payload, classifies each record into specific transaction types, and loads the results into structured tables in a finance database (`finance_db`) using controlled upsert logic.

The resulting data model is designed to support downstream processes such as:
- accounting journal generation  
- subledger (AR/AP) tracking  
- financial statements  
- tax reporting  
- management dashboards  

---

# 🏗️ Architecture Overview

## What the Pipeline Does

```text
database_a                                              finance_db
┌───────────────────────────┐                           ┌──────────────────────────────┐
│ zahir_api_callbacks_raw   │                           │ transaction tables           │
│                           │                           │                              │
│ - callback_id             │                           │ - manual_journal             │
│ - created_at              │──► EXTRACT ──►            │ - sales_invoice              │
│ - end_point               │    PARSE     ──►          │ - sales_return               │
│ - method                  │    STANDARDIZE ──►        │ - receivable_payment         │
│ - body                    │    CLASSIFY  ──►          │ - purchase_invoice           │
│ - processing_status       │    LOAD (upsert) ──►      │ - purchase_return            │
└───────────────────────────┘                           │ - payable_payment            │
                                                        │ - cash_in                    │
                                                        │ - cash_out                   │
                                                        └──────────────┬───────────────┘
                                                                       │
                                                                       ▼
                                                        ┌──────────────────────────────┐
                                                        │ accounting & reporting layer │
                                                        │                              │
                                                        │ - journal_entries            │
                                                        │ - journal_lines              │
                                                        │ - trial_balance              │
                                                        │ - profit_loss                │
                                                        │ - balance_sheet              │
                                                        │ - cash_flow                  │
                                                        │ - supporting_reports         │
                                                        └──────────────────────────────┘
```

> The pipeline is designed to transform raw callback events into structured finance data that can support accounting and reporting processes.

---

# ⚙️ ETL Pipeline

## Extract
Extracts API callback Zahir data from `database_a` for the current month based on the callback timestamp (`created_at`).

The extraction ensures that all transactions within the current accounting period are captured and supports reprocessing if needed.

---

## Transform

1. Parses raw API callback payload (`body`) into structured fields such as:
   - document number  
   - transaction date  
   - partner (customer/vendor)  
   - amount and tax  
   - transaction status  

2. Classifies each callback into specific transaction types based on `end_point` and business logic:
   - manual_journal  
   - sales_invoice  
   - sales_return  
   - receivable_payment  
   - purchase_invoice  
   - purchase_return  
   - payable_payment  
   - cash_in  
   - cash_out  

3. Handles transaction lifecycle changes (e.g. edit, cancellation, reversal) by:
   - updating existing records using upsert logic  
   - recalculating affected values  
   - ensuring the latest transaction state is reflected  

4. Standardizes and enriches data:
   - normalize date, currency, and numeric formats  
   - map references (customer/vendor, document linkage)  
   - preserve raw data for audit trail  

---

## Load (Upsert)

Loads the transformed data into structured finance transaction tables in `finance_db` using controlled upsert logic (`INSERT ... ON DUPLICATE KEY UPDATE` or equivalent).

Re-running the pipeline updates existing records instead of creating duplicates.

Each table must define a **unique business key**, such as:
- `source_callback_id`  
- `document_number`  
- `document_line_id`  

This ensures:
- idempotent processing  
- accurate handling of edits and cancellations  
- full traceability to source data  

---

# 🗄️ Database Setup

## Create Databases

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS database_a;"
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS finance_db;"
```

---

# 📦 Mock Data

The `data/` directory contains SQL seed files to populate both databases.

| File | Target DB | Contents |
|------|-----------|----------|
| `seed_database_a.sql` | `database_a` | Sample raw API callback Zahir data (new, edited, cancelled transactions) |
| `seed_finance_tables.sql` | `finance_db` | Structured transaction tables and required unique keys |

## Re-seed Data

```bash
mysql -u etl_user -p database_a -e "DELETE FROM zahir_api_callbacks_raw;"
mysql -u etl_user -p database_a < data/seed_database_a.sql

mysql -u etl_user -p finance_db -e "
SET FOREIGN_KEY_CHECKS=0;
DELETE FROM cash_out;
DELETE FROM cash_in;
DELETE FROM payable_payment;
DELETE FROM purchase_return;
DELETE FROM purchase_invoice;
DELETE FROM receivable_payment;
DELETE FROM sales_return;
DELETE FROM sales_invoice;
DELETE FROM manual_journal;
SET FOREIGN_KEY_CHECKS=1;
"
mysql -u etl_user -p finance_db < data/seed_finance_tables.sql
```

---

# 🔄 Database Migrations

Migrations are managed with [Alembic](https://alembic.sqlalchemy.org/).

Two migration tracks are used:
- **source** → for raw callback tables (`database_a`)  
- **finance** → for structured transaction and reporting tables (`finance_db`)  

```bash
# Apply all migrations
make migrate

# Apply per database
make migrate-source
make migrate-finance

# Rollback
make rollback-source
make rollback-finance

# Generate migration
make revision-source msg="add index to zahir_api_callbacks_raw"
make revision-finance msg="add unique key to sales_invoice"
```

---

# 🧱 Schema Overview

## Source DB (`database_a`)

- `zahir_api_callbacks_raw`  
  Stores raw API callback data from Zahir.

Key fields:
- callback_id  
- created_at  
- end_point  
- method  
- body (JSON)  
- processing_status  

Purpose:
- single source of truth  
- audit trail  
- reprocessing capability  

---

## Finance DB (`finance_db`)

### 1. Parsed Layer
- `zahir_callbacks_parsed`  
  Standardized callback data (document, partner, amount, references)

---

### 2. Transaction Tables
- `manual_journal`  
- `sales_invoice`  
- `sales_return`  
- `receivable_payment`  
- `purchase_invoice`  
- `purchase_return`  
- `payable_payment`  
- `cash_in`  
- `cash_out`  

Purpose:
- structured transaction representation  
- no aggregation (granular level)  
- foundation for accounting  

---

### 3. Accounting Layer (Planned)
- `journal_entries`  
- `journal_lines`  
- `accounts`  

---

### 4. Reporting Layer (Planned)
- `trial_balance`  
- `profit_loss`  
- `balance_sheet`  
- `cash_flow`  
- `supporting_reports`  

---

# 🧩 Project Structure

```text
.
├── etl_pipeline.py              # Main ETL orchestrator
├── transforms/
│   ├── __init__.py
│   ├── parse_callback.py
│   ├── standardize.py
│   ├── classify.py
│   └── handlers.py              # transaction-specific logic
├── models/
│   ├── source.py
│   └── finance.py
├── data/
│   ├── seed_database_a.sql
│   └── seed_finance_tables.sql
├── alembic/
│   ├── source/
│   └── finance/
├── tests/
│   ├── conftest.py
│   ├── test_etl_pipeline.py
│   ├── test_transforms.py
│   └── test_models.py
├── requirements.txt
├── Makefile
├── .env
└── .env.example
```

---

# 🧠 Key Design Principles

- **Event-driven architecture** (based on API callbacks)  
- **Idempotent processing** (safe re-run)  
- **Auditability** (raw data preserved)  
- **Separation of concerns** (raw → parsed → transaction → reporting)  
- **Extensibility** (ready for accounting & BI integration)  

---

# 🚀 Future Enhancements

- Journal automation (auto-posting rules)  
- AR/AP reconciliation engine  
- Tax reporting module  
- Power BI / dashboard integration  
- Data quality & anomaly detection  

---

# 🏁 Summary

This pipeline evolves from a simple ETL process into a **finance data platform** that:

- transforms raw operational events into structured accounting data  
- supports transaction lifecycle management (edit, cancel, reversal)  
- enables scalable financial reporting automation  
