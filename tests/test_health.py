"""HTTP tests for root, health, and status."""

from fastapi.testclient import TestClient


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "MEME HUNTER AI"
    assert "version" in payload
    assert "secret" not in payload
    assert "private_key" not in payload


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["environment"] == "test"
    assert payload["database_connected"] is True
    assert payload["paper_trading_enabled"] is True
    assert payload["paper_auto_trading"] is False
    assert payload["paper_starting_balance"] == 100


def test_status_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["application"]["name"] == "MEME HUNTER AI"
    assert payload["application"]["status"] == "ok"
    assert payload["application"]["environment"] == "test"
    assert payload["database"]["connected"] is True
    assert payload["database"]["backend"] == "sqlite"
    assert payload["paper_trading"]["enabled"] is True
    assert payload["paper_trading"]["live_trading"] is False
    assert payload["paper_trading"]["mode"] == "paper"
    assert payload["paper_trading"]["starting_balance"] == 100
    assert "private_key" not in str(payload).lower()
    assert "seed" not in str(payload).lower()
