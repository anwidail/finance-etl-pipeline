"""
Shared test fixtures.

Uses SQLite in-memory databases so tests run fast
and don't need a real MySQL server.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.source import SourceBase
from models.finance import FinanceBase


@pytest.fixture
def source_engine():
    """In-memory SQLite engine with source DB tables created."""
    engine = create_engine("sqlite:///:memory:")
    SourceBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def finance_engine():
    """In-memory SQLite engine with finance DB tables created."""
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def source_session(source_engine):
    """SQLAlchemy session bound to the in-memory source DB."""
    Session = sessionmaker(bind=source_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def finance_session(finance_engine):
    """SQLAlchemy session bound to the in-memory finance DB."""
    Session = sessionmaker(bind=finance_engine)
    session = Session()
    yield session
    session.close()
