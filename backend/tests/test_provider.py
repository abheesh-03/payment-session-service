"""Provider integration tests (rollback-isolated, real PostgreSQL).

A CountingProvider replaces the mock via dependency override so invocation
counts can be asserted. Header gating tests use the real mock dependency.
Committed-state, Redis and concurrency provider tests are in test_concurrency.py.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text

from app.api.sessions import get_payment_provider
from app.core.config import get_settings
from app.main import app
from app.repositories.payment_session_repository import PaymentSessionRepository
from app.services.idempotency import digest_key
from tests.fakes import LEAKY_ERROR_TEXT, CountingProvider

CREATE_BODY = {"merchant_reference": "ORDER-5001", "amount": "25.00", "currency": "EUR"}


@pytest.fixture
def provider(api_client: TestClient) -> CountingProvider:
    counting = CountingProvider()
    app.dependency_overrides[get_payment_provider] = lambda: counting
    return counting  # api_client teardown clears all overrides


@pytest.fixture
def mock_failures(api_client: TestClient) -> Iterator[None]:
    enabled = get_settings().model_copy(update={"enable_mock_failures": True})
    app.dependency_overrides[get_settings] = lambda: enabled
    yield


def create_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json=CREATE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def pay(client: TestClient, session_id: str, key: str | None = None, outcome: str | None = None):
    headers = {"Idempotency-Key": key or f"prov-{uuid.uuid4()}"}
    if outcome is not None:
        headers["X-Mock-Provider-Outcome"] = outcome
    return client.post(f"/api/sessions/{session_id}/pay", headers=headers)


def stored(conn: Connection, session_id: str) -> dict:
    return conn.execute(
        text(
            "SELECT status, paid_at, updated_at, created_at, expires_at "
            "FROM payment_sessions WHERE id = :id"
        ),
        {"id": session_id},
    ).one()._asdict()


def receipt_count(conn: Connection, session_id: str) -> int:
    return conn.execute(
        text("SELECT count(*) FROM payment_receipts WHERE session_id = :id"), {"id": session_id}
    ).scalar_one()


def assert_error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == resp.headers["X-Request-ID"]
    return error


# --- success -------------------------------------------------------------------


def test_provider_success_charges_once_with_digest_not_raw_key(
    api_client: TestClient, db_connection: Connection, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)

    resp = pay(api_client, session_id, key="raw-key-123")

    assert resp.status_code == 200
    assert resp.json()["status"] == "PAID"
    assert provider.count == 1
    call = provider.calls[0]
    assert call.session_id == uuid.UUID(session_id)
    assert call.amount == Decimal("25.00") and call.currency == "EUR"
    assert call.idempotency_key_digest == digest_key("raw-key-123")
    assert "raw-key-123" not in repr(call)
    assert receipt_count(db_connection, session_id) == 1


# --- known failure and retry ----------------------------------------------------


def test_known_failure_leaves_session_unchanged_and_creates_no_receipt(
    api_client: TestClient, db_connection: Connection, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)
    before = stored(db_connection, session_id)
    provider.behavior = "unavailable"

    resp = pay(api_client, session_id)

    error = assert_error(resp, 503, "PAYMENT_PROVIDER_UNAVAILABLE")
    assert "same Idempotency-Key" in error["message"]
    assert provider.count == 1
    assert stored(db_connection, session_id) == before  # CREATED; timestamps untouched
    assert receipt_count(db_connection, session_id) == 0


def test_same_key_succeeds_after_known_failure(
    api_client: TestClient, db_connection: Connection, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)
    provider.behavior = "unavailable"
    assert pay(api_client, session_id, key="retry-me").status_code == 503

    provider.behavior = "success"
    resp = pay(api_client, session_id, key="retry-me")

    assert resp.status_code == 200
    assert "Idempotent-Replayed" not in resp.headers  # a new payment, not a replay
    assert provider.count == 2
    row = stored(db_connection, session_id)
    assert row["status"] == "PAID" and row["paid_at"] == row["updated_at"]
    assert receipt_count(db_connection, session_id) == 1


def test_unexpected_provider_error_is_sanitized_and_rolled_back(
    api_client: TestClient,
    db_connection: Connection,
    provider: CountingProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = create_session(api_client)
    before = stored(db_connection, session_id)
    provider.behavior = "crash"

    resp = pay(api_client, session_id, key="crash-key")

    error = assert_error(resp, 500, "INTERNAL_ERROR")
    assert error["message"] == "An unexpected error occurred."
    all_log_data = caplog.text + "".join(str(vars(r)) for r in caplog.records)
    assert LEAKY_ERROR_TEXT not in resp.text and LEAKY_ERROR_TEXT not in all_log_data
    for leaked in ("hunter2", "postgresql://", "RuntimeError", "Traceback"):
        assert leaked not in resp.text
        assert leaked not in all_log_data
    assert stored(db_connection, session_id) == before
    assert receipt_count(db_connection, session_id) == 0

    provider.behavior = "success"
    assert pay(api_client, session_id, key="crash-key").status_code == 200


# --- provider is skipped when no new payment is needed ---------------------------


def test_same_key_replay_skips_provider(
    api_client: TestClient, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)
    first = pay(api_client, session_id, key="replay-key")
    provider.behavior = "unavailable"  # would fail if it were called

    replay = pay(api_client, session_id, key="replay-key")

    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == first.content
    assert provider.count == 1


def test_different_key_on_paid_session_skips_provider(
    api_client: TestClient, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)
    pay(api_client, session_id)

    assert_error(pay(api_client, session_id), 409, "SESSION_ALREADY_PAID")
    assert provider.count == 1


def test_expired_session_skips_provider_and_persists_expiration(
    api_client: TestClient, db_connection: Connection, provider: CountingProvider
) -> None:
    session_id = create_session(api_client)
    db_connection.execute(
        text("UPDATE payment_sessions SET expires_at = clock_timestamp() WHERE id = :id"),
        {"id": session_id},
    )

    assert_error(pay(api_client, session_id), 409, "SESSION_EXPIRED")
    assert provider.count == 0
    assert stored(db_connection, session_id)["status"] == "EXPIRED"
    assert_error(pay(api_client, session_id), 409, "SESSION_EXPIRED")
    assert provider.count == 0


def test_unknown_session_skips_provider(api_client: TestClient, provider: CountingProvider) -> None:
    assert_error(pay(api_client, str(uuid.uuid4())), 404, "SESSION_NOT_FOUND")
    assert provider.count == 0


def test_key_conflict_skips_provider(
    api_client: TestClient, db_connection: Connection, provider: CountingProvider
) -> None:
    session_a = create_session(api_client)
    session_b = create_session(api_client)
    pay(api_client, session_a, key="shared-key")

    assert_error(pay(api_client, session_b, key="shared-key"), 409, "IDEMPOTENCY_CONFLICT")
    assert provider.count == 1
    assert stored(db_connection, session_b)["status"] == "CREATED"


# --- expiration reached during the provider call ---------------------------------


@pytest.fixture
def clock_jump(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Shift the DB clock reading by ``offset`` once the provider has been called.

    Models time passing during the provider call without sleeping: the first
    clock read (before the call) is real, later reads add the offset.
    """
    state = {"offset": timedelta(0)}
    real_current_time = PaymentSessionRepository.current_time
    monkeypatch.setattr(
        PaymentSessionRepository,
        "current_time",
        lambda self: real_current_time(self) + state["offset"],
    )
    return state


def test_expiration_during_provider_call_persists_expired_without_receipt(
    api_client: TestClient, db_connection: Connection, clock_jump: dict
) -> None:
    session_id = create_session(api_client)  # expires in 15 minutes

    def slow_provider(_request) -> None:
        clock_jump["offset"] = timedelta(minutes=20)

    provider = CountingProvider(on_charge=slow_provider)
    app.dependency_overrides[get_payment_provider] = lambda: provider

    resp = pay(api_client, session_id)

    assert_error(resp, 409, "SESSION_EXPIRED")
    assert provider.count == 1
    row = stored(db_connection, session_id)
    assert row["status"] == "EXPIRED"
    assert row["paid_at"] is None
    assert row["updated_at"] >= row["expires_at"]  # stamped with the post-call clock
    assert receipt_count(db_connection, session_id) == 0


def test_paid_at_uses_clock_read_after_provider_returns(
    api_client: TestClient, db_connection: Connection, clock_jump: dict
) -> None:
    session_id = create_session(api_client)

    def slow_provider(_request) -> None:
        clock_jump["offset"] = timedelta(minutes=5)  # still before expiry

    app.dependency_overrides[get_payment_provider] = lambda: CountingProvider(
        on_charge=slow_provider
    )

    assert pay(api_client, session_id).status_code == 200
    row = stored(db_connection, session_id)
    assert row["status"] == "PAID"
    assert row["paid_at"] == row["updated_at"]
    assert row["paid_at"] >= row["created_at"] + timedelta(minutes=5)


# --- X-Mock-Provider-Outcome gating (real mock provider) --------------------------


def test_fail_outcome_rejected_when_mock_failures_disabled(
    api_client: TestClient, db_connection: Connection
) -> None:
    assert get_settings().enable_mock_failures is False
    session_id = create_session(api_client)
    before = stored(db_connection, session_id)

    error = assert_error(pay(api_client, session_id, outcome="fail"), 422, "INVALID_REQUEST")

    assert "ENABLE_MOCK_FAILURES" in error["message"]
    assert stored(db_connection, session_id) == before


@pytest.mark.parametrize("outcome", ["FAIL", "maybe", "", "success,fail"])
def test_unknown_outcome_values_are_rejected(api_client: TestClient, outcome: str) -> None:
    session_id = create_session(api_client)

    error = assert_error(pay(api_client, session_id, outcome=outcome), 422, "INVALID_REQUEST")

    assert error["message"].startswith("X-Mock-Provider-Outcome:")


def test_explicit_success_outcome_pays(api_client: TestClient) -> None:
    session_id = create_session(api_client)
    assert pay(api_client, session_id, outcome="success").status_code == 200


def test_enabled_fail_outcome_returns_503_then_same_key_succeeds(
    api_client: TestClient, db_connection: Connection, mock_failures: None
) -> None:
    session_id = create_session(api_client)

    failed = pay(api_client, session_id, key="gated-key", outcome="fail")
    assert_error(failed, 503, "PAYMENT_PROVIDER_UNAVAILABLE")
    assert stored(db_connection, session_id)["status"] == "CREATED"

    paid = pay(api_client, session_id, key="gated-key")
    assert paid.status_code == 200

    # The real mock would return 503 if it were invoked for this replay.
    replay = pay(api_client, session_id, key="gated-key", outcome="fail")
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == paid.content


# --- logging --------------------------------------------------------------------


def test_provider_failure_logs_are_structured_and_keyless(
    api_client: TestClient, provider: CountingProvider, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = create_session(api_client)
    pay(api_client, session_id, key="secret-key-abc")  # success
    other = create_session(api_client)
    provider.behavior = "unavailable"
    pay(api_client, other, key="secret-key-def")
    provider.behavior = "crash"
    pay(api_client, other, key="secret-key-ghi")

    failures = [
        {k: getattr(r, k) for k in ("session_id", "action", "error_category", "old_status", "new_status")}
        for r in caplog.records
        if r.getMessage() == "payment provider failure"
    ]
    assert failures == [
        {"session_id": other, "action": "pay", "error_category": "provider_unavailable",
         "old_status": "CREATED", "new_status": "CREATED"},
        {"session_id": other, "action": "pay", "error_category": "provider_error",
         "old_status": "CREATED", "new_status": "CREATED"},
    ]
    assert all(getattr(r, "request_id", None) for r in caplog.records if r.getMessage() == "payment provider failure")
    # Neither formatted messages nor structured fields contain a raw key.
    assert "secret-key-" not in caplog.text + "".join(str(vars(r)) for r in caplog.records)
