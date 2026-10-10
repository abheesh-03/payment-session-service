"""Create/fetch payment session API tests against real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.main import app

VALID_BODY = {
    "merchant_reference": "ORDER-1001",
    "amount": "49.99",
    "currency": "USD",
    "description": "Demo purchase",
    "expires_in_minutes": 15,
}

RESPONSE_FIELDS = {
    "id",
    "merchant_reference",
    "amount",
    "currency",
    "description",
    "status",
    "created_at",
    "expires_at",
    "paid_at",
    "updated_at",
    "checkout_url",
}


def create(client: TestClient, **overrides: object):
    return client.post("/api/sessions", json={**VALID_BODY, **overrides})


def assert_error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["request_id"] == resp.headers["X-Request-ID"]
    return body["error"]


# --- create -----------------------------------------------------------------


def test_create_returns_201_with_full_session(api_client: TestClient) -> None:
    resp = create(api_client, merchant_reference="  ORDER-1001  ", currency="usd")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == RESPONSE_FIELDS
    uuid.UUID(body["id"])
    assert body["merchant_reference"] == "ORDER-1001"
    assert body["amount"] == "49.99"
    assert body["currency"] == "USD"
    assert body["description"] == "Demo purchase"
    assert body["status"] == "CREATED"
    assert body["paid_at"] is None
    assert body["checkout_url"] == f"http://localhost:3020/pay/{body['id']}"
    assert resp.headers["X-Request-ID"]


def test_create_timestamps_are_utc_and_expiry_is_applied(api_client: TestClient) -> None:
    before = datetime.now(UTC)
    body = create(api_client, expires_in_minutes=30).json()
    after = datetime.now(UTC)

    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    updated = datetime.fromisoformat(body["updated_at"])
    for value in (body["created_at"], body["expires_at"], body["updated_at"]):
        assert value.endswith("Z")
    assert before <= created <= after
    assert updated == created
    assert expires - created == timedelta(minutes=30)


def test_create_defaults_expiry_to_15_minutes(api_client: TestClient) -> None:
    body = {k: v for k, v in VALID_BODY.items() if k != "expires_in_minutes"}
    resp = api_client.post("/api/sessions", json=body)

    assert resp.status_code == 201
    data = resp.json()
    expiry = datetime.fromisoformat(data["expires_at"]) - datetime.fromisoformat(data["created_at"])
    assert expiry == timedelta(minutes=15)


def test_create_description_is_optional(api_client: TestClient) -> None:
    body = {k: v for k, v in VALID_BODY.items() if k != "description"}
    resp = api_client.post("/api/sessions", json=body)

    assert resp.status_code == 201
    assert resp.json()["description"] is None


def test_create_merchant_reference_need_not_be_unique(api_client: TestClient) -> None:
    first = create(api_client)
    second = create(api_client)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("49.99", "49.99"),
        (49.99, "49.99"),
        ("0.10", "0.10"),
        (10, "10.00"),
        ("10.5", "10.50"),
        ("0.01", "0.01"),
        ("9999999999.99", "9999999999.99"),
    ],
)
def test_create_serializes_money_exactly(
    api_client: TestClient, db_connection: Connection, amount: object, expected: str
) -> None:
    resp = create(api_client, amount=amount)

    assert resp.status_code == 201, resp.text
    assert resp.json()["amount"] == expected
    stored = db_connection.execute(
        text("SELECT amount FROM payment_sessions WHERE id = :id"), {"id": resp.json()["id"]}
    ).scalar_one()
    assert stored == Decimal(expected)


@pytest.mark.parametrize(
    "amount",
    [
        "49.999",  # excessive precision: must be rejected, not rounded
        49.999,
        "0.001",
        0,
        "0.00",
        -5,
        "10000000000.00",  # does not fit NUMERIC(12,2)
        "NaN",
        "Infinity",
        "abc",
        True,
        None,
    ],
)
def test_create_rejects_invalid_amount(api_client: TestClient, amount: object) -> None:
    error = assert_error(create(api_client, amount=amount), 422, "INVALID_REQUEST")
    assert error["message"].startswith("amount:")


@pytest.mark.parametrize("currency", ["JPY", "US", "", "DOLLARS", 840])
def test_create_rejects_unsupported_currency(api_client: TestClient, currency: object) -> None:
    error = assert_error(create(api_client, currency=currency), 422, "INVALID_REQUEST")
    assert error["message"].startswith("currency:")


@pytest.mark.parametrize("currency", ["eur", " gbp ", "Inr"])
def test_create_normalizes_currency(api_client: TestClient, currency: str) -> None:
    resp = create(api_client, currency=currency)

    assert resp.status_code == 201
    assert resp.json()["currency"] == currency.strip().upper()


@pytest.mark.parametrize("reference", ["", "   ", "\t\n", "x" * 101])
def test_create_rejects_invalid_merchant_reference(api_client: TestClient, reference: str) -> None:
    error = assert_error(create(api_client, merchant_reference=reference), 422, "INVALID_REQUEST")
    assert error["message"].startswith("merchant_reference:")


def test_create_requires_merchant_reference(api_client: TestClient) -> None:
    body = {k: v for k, v in VALID_BODY.items() if k != "merchant_reference"}
    assert_error(api_client.post("/api/sessions", json=body), 422, "INVALID_REQUEST")


def test_create_accepts_max_length_fields(api_client: TestClient) -> None:
    resp = create(api_client, merchant_reference="R" * 100, description="d" * 500)
    assert resp.status_code == 201


def test_create_rejects_long_description(api_client: TestClient) -> None:
    error = assert_error(create(api_client, description="d" * 501), 422, "INVALID_REQUEST")
    assert error["message"].startswith("description:")


@pytest.mark.parametrize("minutes", [0, -1, 1441, 1.5, "15", None])
def test_create_rejects_invalid_expiration(api_client: TestClient, minutes: object) -> None:
    error = assert_error(create(api_client, expires_in_minutes=minutes), 422, "INVALID_REQUEST")
    assert error["message"].startswith("expires_in_minutes:")


@pytest.mark.parametrize("minutes", [1, 1440])
def test_create_accepts_expiration_bounds(api_client: TestClient, minutes: int) -> None:
    assert create(api_client, expires_in_minutes=minutes).status_code == 201


def test_create_rejects_client_controlled_fields(api_client: TestClient) -> None:
    error = assert_error(create(api_client, status="PAID"), 422, "INVALID_REQUEST")
    assert error["message"].startswith("status:")


def test_create_rejects_malformed_json(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/sessions", content=b'{"amount": ', headers={"Content-Type": "application/json"}
    )
    assert_error(resp, 422, "INVALID_REQUEST")


def test_checkout_url_uses_configured_frontend_base(api_client: TestClient) -> None:
    custom = get_settings().model_copy(update={"frontend_base_url": "https://pay.example.test"})
    app.dependency_overrides[get_settings] = lambda: custom

    body = create(api_client).json()

    assert body["checkout_url"] == f"https://pay.example.test/pay/{body['id']}"


# --- fetch ------------------------------------------------------------------


def test_fetch_returns_persisted_session(api_client: TestClient, db_connection: Connection) -> None:
    created = create(api_client).json()

    # The row is really in PostgreSQL (read with plain SQL, not the ORM).
    row = db_connection.execute(
        text(
            "SELECT merchant_reference, amount::text AS amount, currency, status, paid_at "
            "FROM payment_sessions WHERE id = :id"
        ),
        {"id": created["id"]},
    ).one()
    assert row._asdict() == {
        "merchant_reference": "ORDER-1001",
        "amount": "49.99",
        "currency": "USD",
        "status": "CREATED",
        "paid_at": None,
    }

    resp = api_client.get(f"/api/sessions/{created['id']}")

    assert resp.status_code == 200
    assert resp.json() == created


def test_fetch_unknown_session_returns_404(api_client: TestClient) -> None:
    error = assert_error(
        api_client.get(f"/api/sessions/{uuid.uuid4()}"), 404, "SESSION_NOT_FOUND"
    )
    assert error["message"] == "Payment session not found."


@pytest.mark.parametrize("session_id", ["not-a-uuid", "123", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"])
def test_fetch_malformed_id_returns_422(api_client: TestClient, session_id: str) -> None:
    error = assert_error(api_client.get(f"/api/sessions/{session_id}"), 422, "INVALID_REQUEST")
    assert error["message"].startswith("session_id:")


# --- request IDs and error format -------------------------------------------


def test_request_id_is_generated_when_absent(api_client: TestClient) -> None:
    first = create(api_client).headers["X-Request-ID"]
    second = create(api_client).headers["X-Request-ID"]
    assert first and second and first != second


def test_valid_caller_request_id_is_echoed(api_client: TestClient) -> None:
    resp = api_client.get(f"/api/sessions/{uuid.uuid4()}", headers={"X-Request-ID": "trace-abc.123"})

    assert resp.headers["X-Request-ID"] == "trace-abc.123"
    assert resp.json()["error"]["request_id"] == "trace-abc.123"


@pytest.mark.parametrize("bad_id", ["x" * 65, "has space", "<script>", "a;b"])
def test_invalid_caller_request_id_is_replaced(api_client: TestClient, bad_id: str) -> None:
    resp = api_client.get(f"/api/sessions/{uuid.uuid4()}", headers={"X-Request-ID": bad_id})

    assert resp.headers["X-Request-ID"] != bad_id
    assert resp.json()["error"]["request_id"] == resp.headers["X-Request-ID"]


def test_unknown_route_uses_error_format(api_client: TestClient) -> None:
    assert_error(api_client.get("/api/nope"), 404, "NOT_FOUND")


# --- database constraints (defense in depth below the API) -------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("amount", "0", "ck_payment_sessions_amount_positive"),
        ("amount", "-1.00", "ck_payment_sessions_amount_positive"),
        ("currency", "JPY", "ck_payment_sessions_currency_supported"),
        ("status", "REFUNDED", "ck_payment_sessions_status_allowed"),
    ],
)
def test_database_rejects_invalid_rows(
    db_connection: Connection, column: str, value: str, constraint: str
) -> None:
    row = {
        "id": uuid.uuid4(),
        "merchant_reference": "DIRECT",
        "amount": "1.00",
        "currency": "USD",
        "status": "CREATED",
        "now": datetime.now(UTC),
        column: value,
    }
    with pytest.raises(IntegrityError, match=constraint), db_connection.begin_nested():
        db_connection.execute(
            text(
                "INSERT INTO payment_sessions "
                "(id, merchant_reference, amount, currency, status, created_at, expires_at, updated_at) "
                "VALUES (:id, :merchant_reference, :amount, :currency, :status, :now, :now, :now)"
            ),
            row,
        )
