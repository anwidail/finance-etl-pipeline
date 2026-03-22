import os
import sys
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from models.source import SourceBase

config = context.config

# Inject env vars into alembic config so %(VAR)s interpolation works
config.set_section_option("alembic", "SOURCE_DB_USER", os.getenv("SOURCE_DB_USER", ""))
config.set_section_option("alembic", "SOURCE_DB_PASSWORD", os.getenv("SOURCE_DB_PASSWORD", ""))
config.set_section_option("alembic", "SOURCE_DB_HOST", os.getenv("SOURCE_DB_HOST", "localhost"))
config.set_section_option("alembic", "SOURCE_DB_PORT", os.getenv("SOURCE_DB_PORT", "3306"))
config.set_section_option("alembic", "SOURCE_DB_NAME", os.getenv("SOURCE_DB_NAME", "database_a"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SourceBase.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode — connects to the database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
