"""HTTP Basic Auth behavior for the Conviction API."""

import base64

from fastapi.testclient import TestClient

import main


def _basic_auth(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_auth_disabled_allows_config(monkeypatch):
    monkeypatch.delenv("CONVICTION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("CONVICTION_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("CONVICTION_AUTH_PASSWORD", raising=False)

    response = TestClient(main.app).get("/api/config")

    assert response.status_code == 200


def test_auth_enabled_rejects_missing_and_wrong_credentials(monkeypatch):
    monkeypatch.setenv("CONVICTION_AUTH_ENABLED", "true")
    monkeypatch.setenv("CONVICTION_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("CONVICTION_AUTH_PASSWORD", "test-password")
    client = TestClient(main.app)

    missing = client.get("/api/config")
    wrong = client.get("/api/config", headers=_basic_auth("test-user", "wrong-password"))

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Basic"
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "Basic"


def test_auth_enabled_without_configured_password_fails_closed(monkeypatch):
    monkeypatch.setenv("CONVICTION_AUTH_ENABLED", "on")
    monkeypatch.setenv("CONVICTION_AUTH_USERNAME", "test-user")
    monkeypatch.delenv("CONVICTION_AUTH_PASSWORD", raising=False)

    response = TestClient(main.app).get(
        "/api/config", headers=_basic_auth("test-user", "test-password")
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_auth_enabled_accepts_correct_credentials(monkeypatch):
    monkeypatch.setenv("CONVICTION_AUTH_ENABLED", "yes")
    monkeypatch.setenv("CONVICTION_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("CONVICTION_AUTH_PASSWORD", "test-password")

    response = TestClient(main.app).get(
        "/api/config", headers=_basic_auth("test-user", "test-password")
    )

    assert response.status_code == 200
