-- ============================================================
-- Seed data for finance_db (destination database)
-- Chart of accounts + unique index for upsert support
--
-- Usage:
--   mysql -u your_user -p finance_db < data/seed_finance_accounts.sql
-- ============================================================

-- -----------------------------------------------------------
-- CHART OF ACCOUNTS
-- -----------------------------------------------------------
INSERT INTO accounts (id, code, name, type) VALUES
(1, '1100', 'Accounts Receivable',   'asset'),
(2, '4100', 'Sales Revenue',         'revenue'),
(3, '2100', 'Tax Payable - PPN',     'liability'),
(4, '5100', 'Cost of Goods Sold',    'expense');

-- -----------------------------------------------------------
-- UNIQUE INDEX for upsert on finance_summary
-- Ensures one row per (date, account) — re-running ETL updates
-- instead of creating duplicates.
-- -----------------------------------------------------------
ALTER TABLE finance_summary
    ADD UNIQUE INDEX uq_summary_date_account (summary_date, account_id);
