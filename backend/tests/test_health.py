"""/health endpoint tests with dependency checks stubbed out."""

import pytest
from fastapi.testclient import TestClient

from app import main


def _fail(*_args: object) -> None:
    raise ConnectionError("postgresql://user:secret@host/db refused")


@pytest.fixture
def client() -> TestClient:
    with TestClient(main.app) as c:
        yield c


def test_health_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "check_database", lambda _engine: None)
    monkeypatch.setattr(main, "check_redis", lambda _client: None)

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


@pytest.mark.parametrize("failing", ["check_database", "check_redis"])
def test_health_dependency_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    monkeypatch.setattr(main, "check_database", lambda _engine: None)
    monkeypatch.setattr(main, "check_redis", lambda _client: None)
    monkeypatch.setattr(main, failing, _fail)

    resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
    assert "unavailable" in resp.json()["checks"].values()
    assert "secret" not in resp.text


def test_cors_allows_frontend_origin_only(client: TestClient) -> None:
    allowed = client.options(
        "/health",
        headers={"Origin": "http://localhost:3020", "Access-Control-Request-Method": "GET"},
    )
    denied = client.options(
        "/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3020"
    assert "access-control-allow-origin" not in denied.headers
