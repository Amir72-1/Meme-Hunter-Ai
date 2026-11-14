"""HTTP tests for discovery and token history endpoints."""

from __future__ import annotations

from sqlalchemy import delete
from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.models import DiscoveryScan, MarketSnapshot, OpportunityAssessment, Token
from tests.dexscreener_fakes import TOKEN_A, boost, pair_payload, profile, ScriptedDex


def _clean() -> None:
    session = get_session_factory()()
    session.execute(delete(MarketSnapshot))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()


def test_discovery_scan_and_token_history(client: TestClient, monkeypatch) -> None:
    _clean()
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.latest_boosts = [boost(TOKEN_A)]
    script.top_boosts = []
    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.02")]
    monkeypatch.setattr("app.api.discovery.DexScreenerClient", script.ctor())

    first = client.post("/api/v1/discovery/scan")
    assert first.status_code == 200
    body = first.json()
    assert body["profiles_fetched"] == 1
    assert body["latest_boosts_fetched"] == 1
    assert body["unique_solana_addresses"] == 1
    assert body["tokens_processed"] == 1
    assert body["snapshots_saved"] == 1
    assert "wallet" not in str(body).lower()

    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.05")]
    second = client.post("/api/v1/discovery/scan")
    assert second.status_code == 200
    assert second.json()["snapshots_saved"] == 1

    listing = client.get("/api/v1/tokens")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert items[0]["token_address"] == TOKEN_A
    assert items[0]["safety_status"] == "unassessed"
    assert items[0]["latest_snapshot"]["price_usd"] == 0.05

    detail = client.get(f"/api/v1/tokens/{TOKEN_A}")
    assert detail.status_code == 200
    assert detail.json()["symbol"] == "MEME"

    history = client.get(f"/api/v1/tokens/{TOKEN_A}/history")
    assert history.status_code == 200
    snapshots = history.json()["items"]
    assert len(snapshots) == 2
    assert snapshots[0]["price_usd"] == 0.02
    assert snapshots[1]["price_usd"] == 0.05
    assert "not complete candles" in history.json()["snapshot_note"].lower()

    missing = client.get("/api/v1/tokens/DoesNotExist111")
    assert missing.status_code == 404

    status_payload = client.get("/api/v1/discovery/status")
    assert status_payload.status_code == 200
    status_body = status_payload.json()
    assert status_body["integration"] == "dexscreener"
    assert status_body["configured"] is True
    assert status_body["live_verified"] is False
    assert status_body["last_scan_status"] == "completed"


def test_discovery_scan_reports_api_failure(client: TestClient, monkeypatch) -> None:
    _clean()
    script = ScriptedDex()
    script.profiles = __import__("httpx").Response(502, json={"error": "bad gateway"})
    monkeypatch.setattr("app.api.discovery.DexScreenerClient", script.ctor())

    response = client.post("/api/v1/discovery/scan")
    assert response.status_code == 502
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["tokens_processed"] == 0
    assert payload["errors"]
