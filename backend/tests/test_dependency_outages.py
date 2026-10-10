"""Real dependency outages (no stubbed checks).

The app is pointed at loopback ports where nothing listens, so the real
psycopg / redis-py clients fail with genuine connection errors. Verifies the
/health contract, the sanitized 503 for database outages, and that payments
stay correct through PostgreSQL when Redis is unreachable.
"""

import socket
import uuid
from collections.abc import Iterator

import pytest
import redis.connection
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine, text

from app.cache.receipt_cache import ReceiptCache
from app.cache.redis_client import (
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    create_redis_client,
)
from app.core.config import get_settings
from app.db.database import create_db_engine, create_session_factory
from app.main import app
from tests.test_concurrency import (  # noqa: F401  (fixtures: live_client, owned, cache)
    Owned,
    cache,
    create_session,
    live_client,
    new_key,
    owned,
    pay,
    receipts_for,
    session_row,
)


def closed_port() -> int:
    """A loopback port with no listener (bound briefly, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def redis_down(client: TestClient) -> Iterator[None]:
    real = client.app.state.redis
    dead = create_redis_client(get_settings().model_copy(update={"redis_port": closed_port()}))
    client.app.state.redis = dead
    try:
        yield
    finally:
        client.app.state.redis = real
        dead.close()


@pytest.fixture
def postgres_down(client: TestClient) -> Iterator[None]:
    real_engine = client.app.state.db_engine
    real_factory = client.app.state.session_factory
    dead = create_db_engine(get_settings().model_copy(update={"postgres_port": closed_port()}))
    client.app.state.db_engine = dead
    client.app.state.session_factory = create_session_factory(dead)
    try:
        yield
    finally:
        client.app.state.db_engine = real_engine
        client.app.state.session_factory = real_factory
        dead.dispose()


def assert_sanitized(text_body: str) -> None:
    settings = get_settings()
    for secret in (
        settings.postgres_password.get_secret_value(),
        settings.postgres_user,
        "postgresql",
        "psycopg",
        "Connection refused",
        "Traceback",
    ):
        assert secret not in text_body


def test_redis_client_fails_fast_without_retries() -> None:
    client = create_redis_client(get_settings())
    try:
        assert client.get_retry().get_retries() == 0
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["retry"].get_retries() == 0
        assert kwargs["socket_connect_timeout"] == REDIS_CONNECT_TIMEOUT_SECONDS <= 2
        assert kwargs["socket_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS <= 2
    finally:
        client.close()


def test_closed_port_makes_one_connection_attempt_per_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts real connection attempts (no timing assertions). redis-py's
    default policy would attempt 11 times per command before giving up."""
    attempts = 0
    real_connect = redis.connection.Connection._connect

    def counting_connect(self: redis.connection.Connection):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        return real_connect(self)  # genuine socket connect to a closed port

    monkeypatch.setattr(redis.connection.Connection, "_connect", counting_connect)
    client = create_redis_client(get_settings().model_copy(update={"redis_port": closed_port()}))
    try:
        with pytest.raises(RedisConnectionError):
            client.get("payment-session-service:test:missing")
        assert attempts == 1

        # The receipt cache treats the failure as a miss (PostgreSQL fallback).
        cache = ReceiptCache(client, prefix="payment-session-service", ttl_seconds=60)
        assert cache.get("0" * 64) is None
        assert attempts == 2
    finally:
        client.close()


def test_health_ok_with_real_dependencies(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


def test_health_503_when_redis_unreachable(client: TestClient, redis_down: None) -> None:
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "checks": {"postgres": "ok", "redis": "unavailable"}}


def test_health_503_when_postgres_unreachable(client: TestClient, postgres_down: None) -> None:
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "checks": {"postgres": "unavailable", "redis": "ok"}}
    assert_sanitized(resp.text)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/api/sessions", {"json": {"merchant_reference": "X", "amount": "1.00", "currency": "USD"}}),
        ("GET", f"/api/sessions/{uuid.uuid4()}", {}),
        ("POST", f"/api/sessions/{uuid.uuid4()}/pay", {"headers": {"Idempotency-Key": "outage-key"}}),
    ],
    ids=["create", "fetch", "pay"],
)
def test_api_returns_sanitized_503_when_postgres_unreachable(
    client: TestClient, postgres_down: None, method: str, path: str, kwargs: dict
) -> None:
    resp = client.request(method, path, **kwargs)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["message"] == "The service is temporarily unavailable."
    assert error["request_id"] == resp.headers["X-Request-ID"]
    assert_sanitized(resp.text)


def test_payment_and_replay_work_with_redis_unreachable(
    live_client: TestClient, engine: Engine, owned: Owned
) -> None:
    """Committed payment through PostgreSQL while Redis really refuses connections."""
    real = live_client.app.state.redis
    dead = create_redis_client(get_settings().model_copy(update={"redis_port": closed_port()}))
    live_client.app.state.redis = dead
    try:
        session_id = create_session(live_client, owned)
        key = new_key(owned)

        first = pay(live_client, session_id, key)
        replay = pay(live_client, session_id, key)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert replay.content == first.content
        assert session_row(engine, session_id)["status"] == "PAID"
        assert len(receipts_for(engine, key=key)) == 1
        with engine.connect() as conn:  # sanity: the database itself is fine
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        live_client.app.state.redis = real
        dead.close()
