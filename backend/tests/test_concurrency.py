"""Idempotency and concurrency against committed PostgreSQL state and real Redis.

Unlike the rollback-based suites, these tests run the real app dependencies:
every request gets its own SQLAlchemy Session from the app's engine (its own
pooled connection) and really commits. Verification and lock holding use
separate connections from a different engine.

Concurrency is proven, not assumed: a "holder" connection takes the lock a
request needs first, both workers are released together through a barrier,
and a monitor connection waits until PostgreSQL reports both worker backends
blocked by the holder (``pg_blocking_pids``). Only then is the holder
released. Every wait is bounded.

Cleanup removes only rows and Redis keys created by each test.
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Connection, Engine, text

from app.api.sessions import get_payment_provider
from app.cache.receipt_cache import ReceiptCache
from app.core.config import get_settings
from app.main import app
from app.services.idempotency import advisory_lock_id, digest_key, pay_fingerprint
from tests.fakes import CountingProvider

PROJECT_REDIS_PORT = 56380
WAIT_TIMEOUT = 10.0
RESULT_TIMEOUT = 30.0
CREATE_BODY = {"merchant_reference": "ORDER-4001", "amount": "75.25", "currency": "INR"}


# --- fixtures ------------------------------------------------------------------


@dataclass
class Owned:
    """Rows and cache keys created by one test, and only those, get cleaned up."""

    session_ids: list[uuid.UUID] = field(default_factory=list)
    key_digests: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def live_client(engine: Engine) -> Iterator[TestClient]:
    # `engine` (conftest) already refused anything but this project's DB.
    settings = get_settings()
    if settings.redis_host not in {"127.0.0.1", "localhost"} or (
        settings.redis_port != PROJECT_REDIS_PORT
    ):
        pytest.exit("Refusing to run Redis tests against a non-project Redis.", returncode=2)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def cache(live_client: TestClient) -> ReceiptCache:
    settings = get_settings()
    return ReceiptCache(
        live_client.app.state.redis,
        prefix=settings.redis_key_prefix,
        ttl_seconds=settings.receipt_cache_ttl_seconds,
    )


@pytest.fixture
def owned(engine: Engine, cache: ReceiptCache) -> Iterator[Owned]:
    owned = Owned()
    yield owned
    with engine.begin() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar_one() == (
            get_settings().postgres_db
        )
        if owned.session_ids:
            ids = {"ids": owned.session_ids}
            conn.execute(text("DELETE FROM payment_receipts WHERE session_id = ANY(:ids)"), ids)
            conn.execute(text("DELETE FROM payment_sessions WHERE id = ANY(:ids)"), ids)
    exact_keys = [cache.key_for(d) for d in owned.key_digests]
    if exact_keys:
        cache._client.delete(*exact_keys)


@pytest.fixture
def provider(live_client: TestClient) -> Iterator[CountingProvider]:
    """Thread-safe counting provider shared by all workers in a test."""
    counting = CountingProvider()
    app.dependency_overrides[get_payment_provider] = lambda: counting
    try:
        yield counting
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)


@pytest.fixture
def monitor(engine: Engine) -> Iterator[Connection]:
    """Autocommit connection so pg_stat_activity is re-read on every query."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        yield conn


# --- helpers -------------------------------------------------------------------


def create_session(client: TestClient, owned: Owned) -> str:
    resp = client.post("/api/sessions", json=CREATE_BODY)
    assert resp.status_code == 201, resp.text
    owned.session_ids.append(uuid.UUID(resp.json()["id"]))
    return resp.json()["id"]


def new_key(owned: Owned) -> str:
    key = f"conc-{uuid.uuid4()}"
    owned.key_digests.append(digest_key(key))
    return key


def pay(client: TestClient, session_id: str, key: str) -> httpx.Response:
    return client.post(f"/api/sessions/{session_id}/pay", headers={"Idempotency-Key": key})


def session_row(engine: Engine, session_id: str) -> dict:
    """Read committed state on a fresh, independent connection."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status, paid_at, updated_at FROM payment_sessions WHERE id = :id"),
            {"id": session_id},
        ).one()._asdict()


def receipts_for(engine: Engine, *, key: str | None = None, session_id: str | None = None):
    with engine.connect() as conn:
        if key is not None:
            where, params = "key_digest = :v", {"v": digest_key(key)}
        else:
            where, params = "session_id = :v", {"v": session_id}
        return [
            r._asdict()
            for r in conn.execute(
                text(
                    "SELECT session_id::text AS session_id, request_fingerprint, "
                    f"response_status, response_body FROM payment_receipts WHERE {where}"
                ),
                params,
            )
        ]


def expire_now(engine: Engine, session_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE payment_sessions SET expires_at = clock_timestamp() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": session_id},
        )


def pay_transitions(caplog: pytest.LogCaptureFixture, session_id: str) -> int:
    return sum(
        1
        for r in caplog.records
        if r.name == "app.services.payment_service"
        and getattr(r, "session_id", None) == session_id
        and getattr(r, "action", None) == "pay"
    )


@dataclass
class Holder:
    pid: int
    release: Callable[[], None]


@contextmanager
def holding_lock(engine: Engine, sql: str, params: dict) -> Iterator[Holder]:
    """Hold a lock in an open transaction on its own connection."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        conn.execute(text(sql), params)
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

        def release() -> None:
            if trans.is_active:
                trans.rollback()

        yield Holder(pid, release)
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


def wait_until_blocked(monitor: Connection, holder_pid: int, count: int) -> list[int]:
    """Bounded poll until ``count`` backends are blocked, directly or through
    another waiter, by the holder. (Row-lock waiters queue behind each other:
    the second waits on the first waiter's tuple lock, not on the holder.)"""
    deadline = time.monotonic() + WAIT_TIMEOUT
    while True:
        pids = monitor.execute(
            text(
                "WITH RECURSIVE blocked(pid) AS ("
                "  SELECT pid FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
                "  UNION"
                "  SELECT a.pid FROM pg_stat_activity a"
                "  JOIN blocked b ON b.pid = ANY(pg_blocking_pids(a.pid))"
                ") SELECT pid FROM blocked ORDER BY pid"
            ),
            {"holder": holder_pid},
        ).scalars().all()
        if len(pids) >= count:
            return list(pids)
        if time.monotonic() > deadline:
            raise AssertionError(f"expected {count} blocked backends, saw {pids}")
        time.sleep(0.01)  # polling interval only; the proof is pg_blocking_pids


def run_concurrently(
    engine: Engine, monitor: Connection, lock_sql: str, lock_params: dict, *calls: Callable
) -> tuple[list[httpx.Response], list[int], int]:
    """Start ``calls`` together while a holder blocks them; release once all wait."""
    barrier = threading.Barrier(len(calls), timeout=WAIT_TIMEOUT)

    def worker(call: Callable) -> httpx.Response:
        barrier.wait()
        return call()

    # The holder context is inner, so it is released before the pool joins.
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        with holding_lock(engine, lock_sql, lock_params) as holder:
            futures = [pool.submit(worker, call) for call in calls]
            blocked = wait_until_blocked(monitor, holder.pid, len(calls))
            holder.release()
        responses = [f.result(timeout=RESULT_TIMEOUT) for f in futures]
    return responses, blocked, holder.pid


ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(:lock_id)"
ROW_LOCK_SQL = "SELECT id FROM payment_sessions WHERE id = :id FOR UPDATE"


# --- 1-3. real concurrency -------------------------------------------------------


def test_same_session_same_key_concurrently(
    live_client: TestClient,
    engine: Engine,
    monitor: Connection,
    owned: Owned,
    caplog: pytest.LogCaptureFixture,
    provider: CountingProvider,
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)

    responses, blocked, holder_pid = run_concurrently(
        engine,
        monitor,
        ADVISORY_LOCK_SQL,
        {"lock_id": advisory_lock_id(digest_key(key))},
        lambda: pay(live_client, session_id, key),
        lambda: pay(live_client, session_id, key),
    )

    # Two distinct worker backends were waiting at the same time.
    assert len(set(blocked)) == 2 and holder_pid not in blocked
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    assert sorted(r.headers.get("Idempotent-Replayed", "false") for r in responses) == [
        "false",
        "true",
    ]
    row = session_row(engine, session_id)
    assert row["status"] == "PAID" and row["paid_at"] == row["updated_at"]
    receipts = receipts_for(engine, key=key)
    assert len(receipts) == 1
    assert receipts[0]["response_body"] == responses[0].json()
    assert pay_transitions(caplog, session_id) == 1
    assert provider.count == 1  # the replaying worker never reached the provider


def test_same_session_different_keys_concurrently(
    live_client: TestClient,
    engine: Engine,
    monitor: Connection,
    owned: Owned,
    caplog: pytest.LogCaptureFixture,
    provider: CountingProvider,
) -> None:
    session_id = create_session(live_client, owned)
    key_a, key_b = new_key(owned), new_key(owned)

    # Different keys take different advisory locks, so both reach the row lock.
    responses, blocked, holder_pid = run_concurrently(
        engine,
        monitor,
        ROW_LOCK_SQL,
        {"id": session_id},
        lambda: pay(live_client, session_id, key_a),
        lambda: pay(live_client, session_id, key_b),
    )

    assert len(set(blocked)) == 2 and holder_pid not in blocked
    by_status = {r.status_code: r for r in responses}
    assert sorted(by_status) == [200, 409]
    assert by_status[409].json()["error"]["code"] == "SESSION_ALREADY_PAID"
    assert session_row(engine, session_id)["status"] == "PAID"
    receipts = receipts_for(engine, session_id=session_id)
    assert len(receipts) == 1
    assert receipts[0]["response_body"] == by_status[200].json()
    assert pay_transitions(caplog, session_id) == 1
    assert provider.count == 1  # the loser saw PAID under the row lock


def test_different_sessions_same_key_concurrently(
    live_client: TestClient,
    engine: Engine,
    monitor: Connection,
    owned: Owned,
    caplog: pytest.LogCaptureFixture,
    provider: CountingProvider,
) -> None:
    session_a = create_session(live_client, owned)
    session_b = create_session(live_client, owned)
    key = new_key(owned)

    responses, blocked, holder_pid = run_concurrently(
        engine,
        monitor,
        ADVISORY_LOCK_SQL,
        {"lock_id": advisory_lock_id(digest_key(key))},
        lambda: pay(live_client, session_a, key),
        lambda: pay(live_client, session_b, key),
    )

    assert len(set(blocked)) == 2 and holder_pid not in blocked
    statuses = [r.status_code for r in responses]
    assert sorted(statuses) == [200, 409]
    winner, loser = (session_a, session_b) if statuses[0] == 200 else (session_b, session_a)
    losing_response = responses[statuses.index(409)]
    assert losing_response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    assert session_row(engine, winner)["status"] == "PAID"
    loser_row = session_row(engine, loser)
    assert loser_row["status"] == "CREATED" and loser_row["paid_at"] is None
    receipts = receipts_for(engine, key=key)
    assert len(receipts) == 1 and receipts[0]["session_id"] == winner
    assert receipts_for(engine, session_id=loser) == []
    assert pay_transitions(caplog, winner) == 1
    assert pay_transitions(caplog, loser) == 0
    assert provider.count == 1 and provider.calls[0].session_id == uuid.UUID(winner)


# --- 4-6. retries, cache loss, Redis failure -------------------------------------


def test_retry_after_discarded_response_returns_stored_result(
    live_client: TestClient, engine: Engine, owned: Owned
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    pay(live_client, session_id, key)  # response "lost"
    committed = session_row(engine, session_id)
    [receipt] = receipts_for(engine, key=key)

    retry = pay(live_client, session_id, key)

    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == receipt["response_body"]
    assert retry.json()["paid_at"] is not None
    assert session_row(engine, session_id) == committed  # original paid_at kept


def test_cache_populated_after_commit_and_deleted_key_falls_back_to_postgres(
    live_client: TestClient, engine: Engine, owned: Owned, cache: ReceiptCache
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    redis_key = cache.key_for(digest_key(key))

    first = pay(live_client, session_id, key)
    assert first.status_code == 200
    ttl = cache._client.ttl(redis_key)
    assert 0 < ttl <= get_settings().receipt_cache_ttl_seconds

    assert cache._client.delete(redis_key) == 1  # this test's exact key only
    retry = pay(live_client, session_id, key)

    assert retry.status_code == 200
    assert retry.content == first.content
    assert cache._client.exists(redis_key) == 1  # refilled from PostgreSQL
    assert len(receipts_for(engine, key=key)) == 1


def test_redis_failures_fall_back_to_postgres(
    live_client: TestClient,
    engine: Engine,
    owned: Owned,
    cache: ReceiptCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = live_client.app.state.redis

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("simulated outage")

    monkeypatch.setattr(redis_client, "get", unavailable)
    monkeypatch.setattr(redis_client, "set", unavailable)
    session_id = create_session(live_client, owned)
    key = new_key(owned)

    first = pay(live_client, session_id, key)
    assert first.status_code == 200
    assert session_row(engine, session_id)["status"] == "PAID"  # write failure undid nothing
    assert len(receipts_for(engine, key=key)) == 1

    replay = pay(live_client, session_id, key)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == first.content

    other = pay(live_client, session_id, new_key(owned))
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "SESSION_ALREADY_PAID"

    monkeypatch.undo()
    assert cache._client.exists(cache.key_for(digest_key(key))) == 0  # nothing was cached


def _other_session_body(entry: str) -> str:
    """Valid shape and fingerprint, but the body claims a different session."""
    data = json.loads(entry)
    data["body"]["id"] = str(uuid.uuid4())
    return json.dumps(data)


@pytest.mark.parametrize(
    "corrupt",
    [lambda _entry: "not json", lambda _entry: '{"status_code": 200}', _other_session_body],
    ids=["not-json", "missing-fields", "body-for-other-session"],
)
def test_malformed_cache_entry_falls_back_to_postgres(
    live_client: TestClient, engine: Engine, owned: Owned, cache: ReceiptCache, corrupt: Callable
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    first = pay(live_client, session_id, key)
    redis_key = cache.key_for(digest_key(key))
    cache._client.set(redis_key, corrupt(cache._client.get(redis_key)), ex=60)

    retry = pay(live_client, session_id, key)

    assert retry.status_code == 200
    assert retry.content == first.content
    assert cache.get(digest_key(key)) is not None  # replaced with a valid entry


def test_cached_receipt_is_not_replayed_for_another_session(
    live_client: TestClient, engine: Engine, owned: Owned, cache: ReceiptCache
) -> None:
    session_a = create_session(live_client, owned)
    session_b = create_session(live_client, owned)
    key = new_key(owned)
    assert pay(live_client, session_a, key).status_code == 200
    cached = cache.get(digest_key(key))
    assert cached is not None and cached.request_fingerprint == pay_fingerprint(uuid.UUID(session_a))

    resp = pay(live_client, session_b, key)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert session_row(engine, session_b)["status"] == "CREATED"


# --- 7-8. key validation and expiration, verified on separate connections ---------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "k" * 129},
        {"Idempotency-Key": "has space"},
        {"Idempotency-Key": "semi;colon"},
        {"Idempotency-Key": "slash/key"},
    ],
    ids=["missing", "empty", "too-long", "space", "semicolon", "slash"],
)
def test_missing_or_invalid_key_is_rejected_without_changes(
    live_client: TestClient, engine: Engine, owned: Owned, headers: dict
) -> None:
    session_id = create_session(live_client, owned)
    before = session_row(engine, session_id)

    resp = live_client.post(f"/api/sessions/{session_id}/pay", headers=headers)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"].startswith("Idempotency-Key:")
    assert error["request_id"] == resp.headers["X-Request-ID"]
    assert session_row(engine, session_id) == before
    assert receipts_for(engine, session_id=session_id) == []


def test_accepts_boundary_keys(live_client: TestClient, engine: Engine, owned: Owned) -> None:
    # A 1-character key passes validation (404 for an unknown session, not 422)
    # without writing anything that could collide with a later run.
    resp = pay(live_client, str(uuid.uuid4()), "k")
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"

    # Full character set and maximum length, each made unique per run.
    for key in [f"Az09._~:-{uuid.uuid4().hex}", ("K" * 96) + uuid.uuid4().hex]:
        owned.key_digests.append(digest_key(key))
        session_id = create_session(live_client, owned)
        assert pay(live_client, session_id, key).status_code == 200
    assert len(key) == 128


def test_expired_pay_commits_expiration_and_does_not_consume_key(
    live_client: TestClient, engine: Engine, owned: Owned, cache: ReceiptCache
) -> None:
    expired_id = create_session(live_client, owned)
    expire_now(engine, expired_id)
    key = new_key(owned)

    resp = pay(live_client, expired_id, key)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SESSION_EXPIRED"
    row = session_row(engine, expired_id)
    assert row["status"] == "EXPIRED" and row["paid_at"] is None
    assert receipts_for(engine, key=key) == []
    assert cache._client.exists(cache.key_for(digest_key(key))) == 0

    # The failed attempt did not consume the key.
    fresh_id = create_session(live_client, owned)
    retry = pay(live_client, fresh_id, key)
    assert retry.status_code == 200
    assert session_row(engine, fresh_id)["status"] == "PAID"
    assert session_row(engine, expired_id)["status"] == "EXPIRED"


# --- provider failures and retries with committed state ----------------------------


def test_known_provider_failure_commits_nothing_and_same_key_retry_succeeds(
    live_client: TestClient,
    engine: Engine,
    owned: Owned,
    cache: ReceiptCache,
    provider: CountingProvider,
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    before = session_row(engine, session_id)
    provider.behavior = "unavailable"

    failed = pay(live_client, session_id, key)

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "PAYMENT_PROVIDER_UNAVAILABLE"
    # Verified on separate connections / Redis: nothing persisted anywhere.
    assert session_row(engine, session_id) == before
    assert receipts_for(engine, key=key) == []
    assert cache._client.exists(cache.key_for(digest_key(key))) == 0

    provider.behavior = "success"
    retried = pay(live_client, session_id, key)

    assert retried.status_code == 200
    assert provider.count == 2
    assert session_row(engine, session_id)["status"] == "PAID"
    assert len(receipts_for(engine, key=key)) == 1
    assert cache._client.exists(cache.key_for(digest_key(key))) == 1


def test_unexpected_provider_error_leaves_no_committed_state(
    live_client: TestClient,
    engine: Engine,
    owned: Owned,
    cache: ReceiptCache,
    provider: CountingProvider,
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    before = session_row(engine, session_id)
    provider.behavior = "crash"

    resp = pay(live_client, session_id, key)

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in resp.text
    assert session_row(engine, session_id) == before
    assert receipts_for(engine, key=key) == []
    assert cache._client.exists(cache.key_for(digest_key(key))) == 0


def test_replay_after_cache_key_deleted_skips_provider(
    live_client: TestClient,
    engine: Engine,
    owned: Owned,
    cache: ReceiptCache,
    provider: CountingProvider,
) -> None:
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    first = pay(live_client, session_id, key)
    assert cache._client.delete(cache.key_for(digest_key(key))) == 1
    provider.behavior = "unavailable"  # would turn a provider call into 503

    replay = pay(live_client, session_id, key)

    assert replay.status_code == 200
    assert replay.content == first.content
    assert provider.count == 1


def test_concurrent_failure_then_waiter_charges_once(
    live_client: TestClient,
    engine: Engine,
    monitor: Connection,
    owned: Owned,
    provider: CountingProvider,
) -> None:
    """Two same-key requests queue on the key lock; a failed first attempt
    leaves no receipt, so the second makes its own (successful) attempt."""
    session_id = create_session(live_client, owned)
    key = new_key(owned)
    outcomes = iter(["unavailable", "success"])

    def flip_behavior(_request) -> None:
        provider.behavior = next(outcomes)

    provider.on_charge = flip_behavior

    responses, blocked, holder_pid = run_concurrently(
        engine,
        monitor,
        ADVISORY_LOCK_SQL,
        {"lock_id": advisory_lock_id(digest_key(key))},
        lambda: pay(live_client, session_id, key),
        lambda: pay(live_client, session_id, key),
    )

    assert len(set(blocked)) == 2 and holder_pid not in blocked
    assert sorted(r.status_code for r in responses) == [200, 503]
    assert provider.count == 2
    assert session_row(engine, session_id)["status"] == "PAID"
    assert len(receipts_for(engine, key=key)) == 1
