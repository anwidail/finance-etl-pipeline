.PHONY: venv install run test clean migrate-source migrate-finance migrate rollback-source rollback-finance revision-source revision-finance

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

clean:
	rm -rf venv __pycache__ *.log
