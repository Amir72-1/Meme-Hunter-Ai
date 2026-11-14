"""HTTP tests for intelligence endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database import get_session_factory
from app.models import (
    IntelligenceScan,
    Narrative,
    NarrativeMention,
    NarrativeScoreHistory,
    NewsItem,
    SocialPost,
    TokenNarrativeMatch,
)
from app.services.news_collector import NewsScanResult
from app.services.social_adapters import AdapterStatus, NOT_CONFIGURED, SocialScanResult


def _clean() -> None:
    session = get_session_factory()()
    session.execute(delete(TokenNarrativeMatch))
    session.execute(delete(NarrativeScoreHistory))
    session.execute(delete(NarrativeMention))
    session.execute(delete(SocialPost))
    session.execute(delete(NewsItem))
    session.execute(delete(Narrative))
    session.execute(delete(IntelligenceScan))
    session.commit()
    session.close()


def test_news_scan_endpoint(client: TestClient, monkeypatch) -> None:
    _clean()

    async def fake_collect(session, settings=None, client=None, feeds=None):
        session.add(
            NewsItem(
                title="Solana ecosystem update",
                source="CoinDesk",
                url="https://example.com/solana",
                published_at=datetime.now(timezone.utc),
                summary="Crypto ecosystem development on Solana.",
                category="cryptocurrency",
                normalized_title="solana ecosystem update",
            )
        )
        session.commit()
        return NewsScanResult(
            status="completed",
            feeds_attempted=1,
            feeds_ok=1,
            items_fetched=1,
            items_saved=1,
            duplicates_skipped=0,
        )

    monkeypatch.setattr("app.api.intelligence.collect_news", fake_collect)
    response = client.post("/api/v1/intelligence/news/scan")
    assert response.status_code == 200
    assert response.json()["items_saved"] == 1
    listing = client.get("/api/v1/intelligence/news")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_social_and_status_endpoints(client: TestClient, monkeypatch) -> None:
    _clean()

    async def fake_social(session, settings=None, client=None):
        return SocialScanResult(
            status="completed",
            adapters=[
                AdapterStatus("reddit", NOT_CONFIGURED, detail="missing"),
                AdapterStatus("x", NOT_CONFIGURED, detail="missing"),
                AdapterStatus("telegram", NOT_CONFIGURED, detail="missing"),
            ],
        )

    monkeypatch.setattr("app.api.intelligence.collect_social", fake_social)
    social = client.post("/api/v1/intelligence/social/scan")
    assert social.status_code == 200
    assert social.json()["items_saved"] == 0
    assert all(item["status"] == NOT_CONFIGURED for item in social.json()["adapters"])

    status_payload = client.get("/api/v1/intelligence/status")
    assert status_payload.status_code == 200
    body = status_payload.json()
    assert body["social"]["reddit"] == NOT_CONFIGURED
    assert body["social"]["x"] == NOT_CONFIGURED


def test_narratives_scan_endpoint(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    session.add(
        NewsItem(
            title="OpenAI and ChatGPT headlines",
            source="BBC Technology",
            url="https://example.com/openai",
            published_at=datetime.now(timezone.utc),
            summary="Artificial intelligence product launch.",
            category="artificial_intelligence",
            normalized_title="openai and chatgpt headlines",
        )
    )
    session.commit()
    session.close()
    response = client.post("/api/v1/intelligence/narratives/scan")
    assert response.status_code == 200
    payload = response.json()
    assert payload["narratives_upserted"] >= 1
    listing = client.get("/api/v1/intelligence/narratives")
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1
    narrative_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/api/v1/intelligence/narratives/{narrative_id}")
    assert detail.status_code == 200
    assert "mention_count" in detail.json()
