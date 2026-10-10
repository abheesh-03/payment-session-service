"""Fixtures for API tests against this project's real PostgreSQL.

Isolation: each test runs inside one outer transaction on a dedicated
connection. Every request gets its own Session joined to that connection with
``join_transaction_mode="create_savepoint"``, so the service's commit only
releases a savepoint. The outer transaction is rolled back after the test, so
nothing is ever persisted and no table is dropped or truncated.

Because those "commits" are rolled back, the Redis receipt cache is disabled
for ``api_client``: caching them would cache results that never committed.
Committed-transaction and Redis behavior is covered in test_concurrency.py.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.orm import Session

from app.api.sessions import get_receipt_cache
from app.core.config import get_settings
from app.db.database import create_db_engine, get_db
from app.main import app

# This project's PostgreSQL is published on a fixed loopback-only host port.
PROJECT_DB_PORT = 55432
EXPECTED_REVISION = "0002"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    settings = get_settings()
    if settings.postgres_host not in {"127.0.0.1", "localhost"} or (
        settings.postgres_port != PROJECT_DB_PORT
    ):
        pytest.exit("Refusing to run DB tests against a non-project PostgreSQL.", returncode=2)

    engine = create_db_engine(settings)
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            pytest.exit("Run `alembic upgrade head` before the tests.", returncode=2)
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if revision != EXPECTED_REVISION:
            pytest.exit(f"Expected migration {EXPECTED_REVISION}, found {revision}.", returncode=2)
    yield engine
    engine.dispose()


@pytest.fixture
def db_connection(engine: Engine) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def api_client(db_connection: Connection) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        with Session(
            bind=db_connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        ) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_receipt_cache] = lambda: None
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
