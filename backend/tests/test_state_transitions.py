"""Payment and lazy-expiration tests against real PostgreSQL.

Expiration is controlled by rewriting ``expires_at`` relative to the database
clock (no sleeps). Persisted state is always re-read with plain SQL.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from app.core.errors import SessionAlreadyPaidError, SessionExpiredError
from app.providers.mock import MockPaymentProvider
from app.schemas.payment_session import CreatePaymentSessionRequest
from app.services.payment_service import PaymentService

CREATE_BODY = {"merchant_reference": "ORDER-2001", "amount": "25.00", "currency": "EUR"}


# --- helpers -----------------------------------------------------------------


def create_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json=CREATE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def pay(client: TestClient, session_id: str, key: str | None = None, **kwargs):
    """POST /pay with an explicit Idempotency-Key (a fresh one unless given)."""
    headers = {"Idempotency-Key": key or f"test-{uuid.uuid4()}", **kwargs.pop("headers", {})}
    return client.post(f"/api/sessions/{session_id}/pay", headers=headers, **kwargs)


def set_expires_at(conn: Connection, session_id: str, offset: str) -> None:
    """Set expires_at to the DB clock plus ``offset`` (e.g. '-1 minute')."""
    conn.execute(
        text(
            "UPDATE payment_sessions "
            "SET expires_at = clock_timestamp() + CAST(:offset AS interval) WHERE id = :id"
        ),
        {"id": session_id, "offset": offset},
    )


def force_status(conn: Connection, session_id: str, status: str, paid: bool = False) -> None:
    """Simulate a change committed by another request."""
    conn.execute(
        text(
            "UPDATE payment_sessions SET status = :status, updated_at = clock_timestamp(), "
            "paid_at = CASE WHEN :paid THEN clock_timestamp() ELSE NULL END WHERE id = :id"
        ),
        {"id": session_id, "status": status, "paid": paid},
    )


def stored(conn: Connection, session_id: str) -> dict:
    row = conn.execute(
        text(
            "SELECT status, paid_at, updated_at, created_at, expires_at "
            "FROM payment_sessions WHERE id = :id"
        ),
        {"id": session_id},
    ).one()
    return row._asdict()


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def assert_error(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == resp.headers["X-Request-ID"]


# --- pay ---------------------------------------------------------------------


def test_created_session_can_be_paid(api_client: TestClient, db_connection: Connection) -> None:
    session_id = create_session(api_client)
    # The outer test transaction started before the request; now() would be
    # frozen at that instant, clock_timestamp() must be later.
    txn_start = db_connection.execute(text("SELECT now()")).scalar_one()

    resp = pay(api_client, session_id)

    assert resp.status_code == 200, resp.text
    assert "Idempotent-Replayed" not in resp.headers
    body = resp.json()
    row = stored(db_connection, session_id)
    assert body["status"] == row["status"] == "PAID"
    assert row["paid_at"] is not None
    assert row["paid_at"] == row["updated_at"]
    assert row["paid_at"] > txn_start
    assert body["paid_at"] == body["updated_at"] == iso(row["paid_at"])
    assert body["created_at"] == iso(row["created_at"])
    assert body["checkout_url"].endswith(f"/pay/{session_id}")


def test_pay_response_matches_subsequent_fetch(api_client: TestClient) -> None:
    session_id = create_session(api_client)

    paid = pay(api_client, session_id).json()
    fetched = api_client.get(f"/api/sessions/{session_id}")

    assert fetched.status_code == 200
    assert fetched.json() == paid


def test_pay_with_different_key_returns_409_and_preserves_paid_at(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    pay(api_client, session_id, key="first-key")
    after_first = stored(db_connection, session_id)

    resp = pay(api_client, session_id, key="second-key")

    assert_error(resp, 409, "SESSION_ALREADY_PAID")
    assert stored(db_connection, session_id) == after_first


def test_pay_with_same_key_replays_without_new_transition(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    first = pay(api_client, session_id, key="same-key")
    after_first = stored(db_connection, session_id)

    replay = pay(api_client, session_id, key="same-key")

    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == first.content
    assert stored(db_connection, session_id) == after_first


def test_pay_ignores_client_supplied_status_and_timestamps(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)

    resp = pay(api_client, session_id, json={"status": "EXPIRED", "paid_at": "2000-01-01T00:00:00Z"})

    assert resp.status_code == 200
    row = stored(db_connection, session_id)
    assert row["status"] == "PAID"
    assert row["paid_at"].year != 2000


# --- lazy expiration -----------------------------------------------------------


def test_get_expires_overdue_created_session(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "-1 minute")
    before = stored(db_connection, session_id)

    resp = api_client.get(f"/api/sessions/{session_id}")

    assert resp.status_code == 200
    row = stored(db_connection, session_id)
    assert resp.json()["status"] == row["status"] == "EXPIRED"
    assert row["paid_at"] is None and resp.json()["paid_at"] is None
    assert row["updated_at"] > before["updated_at"]
    assert resp.json()["updated_at"] == iso(row["updated_at"])


def test_expiry_is_inclusive_at_expires_at(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "0 seconds")

    assert api_client.get(f"/api/sessions/{session_id}").json()["status"] == "EXPIRED"


def test_get_leaves_unexpired_session_unchanged(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "1 minute")
    before = stored(db_connection, session_id)

    resp = api_client.get(f"/api/sessions/{session_id}")

    assert resp.json()["status"] == "CREATED"
    assert stored(db_connection, session_id) == before


def test_paying_overdue_session_returns_409_and_persists_expired(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "-1 second")

    resp = pay(api_client, session_id)

    assert_error(resp, 409, "SESSION_EXPIRED")
    # The expiration was committed even though the request failed.
    row = stored(db_connection, session_id)
    assert row["status"] == "EXPIRED"
    assert row["paid_at"] is None
    assert row["updated_at"] >= row["expires_at"]


def test_already_expired_session_cannot_be_paid(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "-1 minute")
    api_client.get(f"/api/sessions/{session_id}")
    expired = stored(db_connection, session_id)
    assert expired["status"] == "EXPIRED"

    resp = pay(api_client, session_id)

    assert_error(resp, 409, "SESSION_EXPIRED")
    assert stored(db_connection, session_id) == expired


def test_paid_session_stays_paid_after_expiry_passes(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    pay(api_client, session_id)
    set_expires_at(db_connection, session_id, "-1 hour")
    paid = stored(db_connection, session_id)

    resp = api_client.get(f"/api/sessions/{session_id}")

    assert resp.json()["status"] == "PAID"
    assert stored(db_connection, session_id) == paid
    assert_error(pay(api_client, session_id), 409, "SESSION_ALREADY_PAID")
    assert stored(db_connection, session_id) == paid


def test_repeated_fetches_of_expired_session_do_not_change_it(
    api_client: TestClient, db_connection: Connection
) -> None:
    session_id = create_session(api_client)
    set_expires_at(db_connection, session_id, "-1 minute")
    first = api_client.get(f"/api/sessions/{session_id}").json()
    after_first = stored(db_connection, session_id)

    second = api_client.get(f"/api/sessions/{session_id}").json()
    third = api_client.get(f"/api/sessions/{session_id}").json()

    assert first == second == third
    assert stored(db_connection, session_id) == after_first


# --- stale callers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "paid", "expected_code"),
    [("PAID", True, "SESSION_ALREADY_PAID"), ("EXPIRED", False, "SESSION_EXPIRED")],
)
def test_stale_client_cannot_overwrite_terminal_state(
    api_client: TestClient,
    db_connection: Connection,
    status: str,
    paid: bool,
    expected_code: str,
) -> None:
    session_id = create_session(api_client)
    # Client saw CREATED...
    assert api_client.get(f"/api/sessions/{session_id}").json()["status"] == "CREATED"
    # ...then another request moved the session to a terminal state.
    force_status(db_connection, session_id, status, paid=paid)
    terminal = stored(db_connection, session_id)

    resp = pay(api_client, session_id)

    assert_error(resp, 409, expected_code)
    assert stored(db_connection, session_id) == terminal


@pytest.mark.parametrize(
    ("status", "paid", "error"),
    [("PAID", True, SessionAlreadyPaidError), ("EXPIRED", False, SessionExpiredError)],
)
def test_locked_read_ignores_stale_identity_map(
    db_connection: Connection, status: str, paid: bool, error: type[Exception]
) -> None:
    """A Session holding an old CREATED copy must still decide on the locked row."""
    with Session(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    ) as db:
        service = PaymentService(db)
        stale = service.create_session(CreatePaymentSessionRequest(**CREATE_BODY))
        assert stale.status == "CREATED"
        force_status(db_connection, str(stale.id), status, paid=paid)
        terminal = stored(db_connection, str(stale.id))

        with pytest.raises(error):
            service.pay_session(
                stale.id, "stale-client-key", MockPaymentProvider(), "http://localhost:3020"
            )

        assert stale.status == status  # refreshed from the locked SELECT
        assert stored(db_connection, str(stale.id)) == terminal


# --- error contract ------------------------------------------------------------


def test_pay_unknown_session_returns_404(api_client: TestClient) -> None:
    assert_error(pay(api_client, str(uuid.uuid4())), 404, "SESSION_NOT_FOUND")


@pytest.mark.parametrize("session_id", ["not-a-uuid", "123"])
def test_pay_malformed_id_returns_422(api_client: TestClient, session_id: str) -> None:
    resp = pay(api_client, session_id)
    assert_error(resp, 422, "INVALID_REQUEST")
    assert resp.json()["error"]["message"].startswith("session_id:")


def test_pay_requires_post(api_client: TestClient) -> None:
    session_id = create_session(api_client)
    assert_error(api_client.get(f"/api/sessions/{session_id}/pay"), 405, "METHOD_NOT_ALLOWED")


# --- transition logging --------------------------------------------------------


def test_transitions_are_logged_with_context(
    api_client: TestClient, db_connection: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    paid_id = create_session(api_client)
    expired_id = create_session(api_client)
    set_expires_at(db_connection, expired_id, "-1 minute")

    pay(api_client, paid_id, key="log-key", headers={"X-Request-ID": "req-pay"})
    pay(api_client, paid_id, key="log-key")  # replay: no transition
    pay(api_client, paid_id)  # different key, rejected: no transition
    pay(api_client, expired_id, headers={"X-Request-ID": "req-exp"})
    api_client.get(f"/api/sessions/{expired_id}")  # already expired: no transition

    fields = [
        {k: getattr(r, k) for k in ("request_id", "session_id", "action", "old_status", "new_status")}
        for r in caplog.records
        if r.name == "app.services.payment_service"
    ]
    assert fields == [
        {
            "request_id": "req-pay",
            "session_id": paid_id,
            "action": "pay",
            "old_status": "CREATED",
            "new_status": "PAID",
        },
        {
            "request_id": "req-exp",
            "session_id": expired_id,
            "action": "expire",
            "old_status": "CREATED",
            "new_status": "EXPIRED",
        },
    ]
