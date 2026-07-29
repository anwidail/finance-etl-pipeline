.PHONY: venv install run test clean migrate-source migrate-finance migrate rollback-source rollback-finance revision-source revision-finance cost-update cost-check cost-seed cost-close cost-reopen migrate-cost sales-post sales-status sales-check sales-import

venv:
	python3 -m venv venv

install: venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt

run:
	venv/bin/python etl_pipeline.py

test:
	venv/bin/pytest tests/ -v

# --- Migrations ---

# Apply all pending migrations to both databases
migrate: migrate-finance

migrate-finance:
	venv/bin/alembic -c alembic/finance/alembic.ini upgrade head

# Rollback the last migration
rollback-finance:
	venv/bin/alembic -c alembic/finance/alembic.ini downgrade -1

# Auto-generate a new migration from model changes
# Usage: make revision-source msg="add index to transactions"
revision-finance:
	venv/bin/alembic -c alembic/finance/alembic.ini revision --autogenerate -m "$(msg)"

# --- Sales detail (manually imported sales invoices) ---

# Post the gl journal from whatever is currently in sales_detail. Idempotent:
# run it after every import or edit. Replaces each invoice's entry rather than
# appending, so re-running corrects instead of duplicating.
sales-post:
	venv/bin/python -m load.sales_detail_loader --post

# Which invoices are unposted, stale, or orphaned? Writes nothing; exits 1 when
# the ledger has fallen behind the table.
sales-status:
	venv/bin/python -m load.sales_detail_loader --status

# Validate without writing.
sales-check:
	venv/bin/python -m load.sales_detail_loader --post --dry-run

# Import the workbook itself (fills sales_detail AND posts the journal).
# Usage: make sales-import FILE=sales_detail.xlsx
sales-import:
	venv/bin/python -m load.sales_detail_loader --file $(or $(FILE),sales_detail.xlsx)

# --- Cost distribution ---

# Full refresh for one month: picks up edits to basis_fte/basis_rev AND new
# postings in accounting.gl, then rewrites that period's snapshot.
# Usage: make cost-update P=JUL-2026
cost-update:
	@test -n "$(P)" || { echo "Usage: make cost-update P=JUL-2026"; exit 1; }
	venv/bin/python -m cost_distribution.pipeline --rebuild-allocation --period $(P)
	venv/bin/python -m cost_distribution.pipeline --import-gl-from-accounting --period $(P)
	venv/bin/python -m cost_distribution.pipeline --verify-basis --period $(P)
	venv/bin/python -m cost_distribution.pipeline --period $(P) --gl-from-db --basis-from-db --to-db

# Read-only: validate the same run without writing anything.
# Usage: make cost-check P=JUL-2026
cost-check:
	@test -n "$(P)" || { echo "Usage: make cost-check P=JUL-2026"; exit 1; }
	venv/bin/python -m cost_distribution.pipeline --verify-basis --period $(P)
	venv/bin/python -m cost_distribution.pipeline --period $(P) --gl-from-db --basis-from-db --dry-run

# Seed a brand-new month from the workbook. Run ONCE, before editing the basis —
# it replaces that period's basis_allocation/basis_fte/basis_rev.
# Usage: make cost-seed P=AUG-2026
cost-seed:
	@test -n "$(P)" || { echo "Usage: make cost-seed P=AUG-2026"; exit 1; }
	venv/bin/python -m cost_distribution.pipeline --import-basis --period $(P)

# Usage: make cost-close P=JUL-2026 note="audit sign-off"
cost-close:
	@test -n "$(P)" || { echo "Usage: make cost-close P=JUL-2026 note=\"...\""; exit 1; }
	venv/bin/python -m cost_distribution.pipeline --close-period --period $(P) --note "$(note)"

cost-reopen:
	@test -n "$(P)" || { echo "Usage: make cost-reopen P=JUL-2026"; exit 1; }
	venv/bin/python -m cost_distribution.pipeline --reopen-period --period $(P)

migrate-cost:
	venv/bin/alembic -c alembic/cost/alembic.ini upgrade head

clean:
	rm -rf venv __pycache__ *.log
