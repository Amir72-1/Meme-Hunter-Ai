"""HTTP tests for opportunity ranking endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import get_session_factory
from app.models import (
    IntelligenceScan,
    MarketSnapshot,
    OpportunityAssessment,
    Token,
    utc_now,
)

TOKEN_A = "OppApiA11111111111111111111111111111111111"
TOKEN_B = "OppApiB22222222222222222222222222222222222"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _clean() -> None:
    session = get_session_factory()()
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(Token))
    session.execute(delete(IntelligenceScan))
    session.commit()
    session.close()


def _seed() -> None:
    session = get_session_factory()()
    now = utc_now()
    high = Token(token_address=TOKEN_A, name="Alpha", symbol="ALP", chain="solana")
    low = Token(token_address=TOKEN_B, name="Beta", symbol="BET", chain="solana")
    session.add_all([high, low])
    session.commit()
    session.add(
        MarketSnapshot(
            token_id=high.id,
            liquidity_usd=25000,
            volume_24h=9000,
            recorded_at=now,
        )
    )
    session.add(
        MarketSnapshot(
            token_id=low.id,
            liquidity_usd=3000,
            volume_24h=400,
            recorded_at=now,
        )
    )
    session.add(
        OpportunityAssessment(
            token_id=high.id,
            assessed_at=now,
            status="SCORED",
            ranking_label="HIGH_OPPORTUNITY",
            opportunity_score=78,
            positive_opportunity_score=82,
            security_adjustment=-4,
            confidence=0.72,
            data_completeness=0.8,
            available_components_json='["market_momentum","liquidity_quality"]',
            missing_components_json='["narrative_momentum"]',
            component_scores_json="[]",
            positive_signals_json='["volume growth"]',
            negative_signals_json="[]",
            warnings_json="[]",
            explanation="SCORED/HIGH_OPPORTUNITY. Paper intelligence, not a prediction of profit.",
            snapshot_count=1,
            last_snapshot_at=now,
        )
    )
    session.add(
        OpportunityAssessment(
            token_id=low.id,
            assessed_at=now - timedelta(minutes=1),
            status="INSUFFICIENT_DATA",
            ranking_label="INSUFFICIENT_DATA",
            opportunity_score=30,
            positive_opportunity_score=30,
            security_adjustment=0,
            confidence=0.2,
            data_completeness=0.2,
            available_components_json='["liquidity_quality"]',
            missing_components_json='["market_momentum","narrative_momentum","early_discovery","security_adjustment"]',
            component_scores_json="[]",
            positive_signals_json="[]",
            negative_signals_json="[]",
            warnings_json='["thin coverage"]',
            explanation="INSUFFICIENT_DATA",
            snapshot_count=1,
            last_snapshot_at=now - timedelta(minutes=1),
        )
    )
    session.commit()
    session.close()


def test_opportunity_api_scan_list_filters_and_history(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    token = Token(
        token_address=TOKEN_A,
        name="Alpha",
        symbol="ALP",
        chain="solana",
        pair_created_at=utc_now() - timedelta(hours=8),
        last_discovered_at=utc_now() - timedelta(hours=8),
    )
    session.add(token)
    session.commit()
    session.add(
        MarketSnapshot(
            token_id=token.id,
            price_usd=0.02,
            liquidity_usd=22000,
            volume_24h=7000,
            buys_24h=30,
            sells_24h=12,
            recorded_at=utc_now() - timedelta(hours=2),
        )
    )
    session.add(
        MarketSnapshot(
            token_id=token.id,
            price_usd=0.03,
            liquidity_usd=24000,
            volume_24h=11000,
            buys_24h=40,
            sells_24h=10,
            recorded_at=utc_now(),
        )
    )
    session.commit()
    session.close()

    scanned = client.post("/api/v1/opportunities/scan", json={"limit": 10, "force": True})
    assert scanned.status_code == 200
    body = scanned.json()
    assert body["status"] == "completed"
    assert body["tokens_scanned"] == 1
    assert "low_confidence" in body
    assert "scored" in body
    assert "reliable_classifications" in body
    assert body["scored"] + body["low_confidence"] + body["insufficient_data"] == body["tokens_scanned"]
    assert body["reliable_classifications"] == body["scored"]
    assert "profit" in body["safety_note"].lower() or "paper" in body["safety_note"].lower()
    assert "private_key" not in str(body).lower()

    status_payload = client.get("/api/v1/opportunities/status")
    assert status_payload.status_code == 200
    assert status_payload.json()["last_scan_status"] == "completed"

    listed = client.get("/api/v1/opportunities")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assessment = item["assessment"]
    for key in (
        "opportunity_score",
        "positive_opportunity_score",
        "security_adjustment",
        "confidence",
        "data_completeness",
        "status",
        "available_components",
        "missing_components",
        "positive_signals",
        "negative_signals",
        "warnings",
        "explanation",
        "assessed_at",
        "ranking_label",
    ):
        assert key in assessment
    assert item["token_address"] == TOKEN_A
    assert item["snapshot_count"] == 2
    assert item["last_snapshot_at"] is not None
    assert assessment["narrative_match_count"] == 0
    assert assessment["security_coverage"] in {"UNAVAILABLE", "UNCONFIRMED", "CONFIRMED"}
    assert assessment["snapshot_count"] == 2
    assert item["freshness"]["is_stale"] is False
    assert item["freshness"]["current_snapshot_count"] == 2
    assert item["freshness"]["assessment_snapshot_count"] == 2
    assert item["freshness"]["source"] == "persisted"
    assert "guarantee" in item["safety_note"].lower() or "not a guarantee" in item["safety_note"].lower()

    filtered = client.get("/api/v1/opportunities", params={"min_liquidity_usd": 100000})
    assert filtered.json()["total"] == 0

    detail = client.get(f"/api/v1/opportunities/{TOKEN_A}")
    assert detail.status_code == 200
    assert detail.json()["assessment"]["status"] in {"SCORED", "LOW_CONFIDENCE", "INSUFFICIENT_DATA"}

    missing = client.get("/api/v1/opportunities/DoesNotExist111")
    assert missing.status_code == 404

    client.post("/api/v1/opportunities/scan", json={"limit": 10, "force": True})
    history = client.get(f"/api/v1/opportunities/{TOKEN_A}/history")
    assert history.status_code == 200
    assert len(history.json()["items"]) == 2

    session = get_session_factory()()
    stored = list(session.scalars(select(OpportunityAssessment)).all())
    assert len(stored) == 2
    session.close()


def test_opportunity_list_pagination_and_status_filter(client: TestClient) -> None:
    _clean()
    _seed()
    first = client.get("/api/v1/opportunities", params={"limit": 1, "offset": 0})
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["token_address"] == TOKEN_A

    second = client.get("/api/v1/opportunities", params={"limit": 1, "offset": 1})
    assert second.json()["items"][0]["token_address"] == TOKEN_B

    scored = client.get("/api/v1/opportunities", params={"assessment_status": "SCORED"})
    assert scored.json()["total"] == 1
    assert scored.json()["items"][0]["assessment"]["status"] == "SCORED"

    excluded = client.get("/api/v1/opportunities", params={"exclude_insufficient": True})
    assert excluded.json()["total"] == 1
    assert excluded.json()["items"][0]["token_address"] == TOKEN_A

    confident = client.get("/api/v1/opportunities", params={"min_confidence": 0.5})
    assert confident.json()["total"] == 1


def test_historical_unreliable_row_cannot_bypass_current_rules(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    token = Token(token_address=TOKEN_A, name="Alpha", symbol="ALP", chain="solana")
    session.add(token)
    session.commit()
    session.add(
        OpportunityAssessment(
            token_id=token.id,
            assessed_at=NOW,
            status="SCORED",
            ranking_label="HIGH_OPPORTUNITY",
            opportunity_score=68.6,
            positive_opportunity_score=68.6,
            security_adjustment=0,
            confidence=0.41,
            data_completeness=0.35,
            available_components_json='["market_momentum","liquidity_quality"]',
            missing_components_json='["narrative_momentum","early_discovery","security_adjustment"]',
            component_scores_json="[]",
            positive_signals_json="[]",
            negative_signals_json="[]",
            warnings_json="[]",
            explanation="legacy SCORED/HIGH_OPPORTUNITY",
        )
    )
    session.commit()
    session.close()

    detail = client.get(f"/api/v1/opportunities/{TOKEN_A}")
    assert detail.status_code == 200
    assessment = detail.json()["assessment"]
    assert assessment["positive_opportunity_score"] == 68.6
    assert assessment["opportunity_score"] == 68.6
    assert assessment["confidence"] == 0.41
    assert assessment["data_completeness"] == 0.35
    assert assessment["status"] == "INSUFFICIENT_DATA"
    assert assessment["ranking_label"] == "INSUFFICIENT_DATA"

    listed = client.get("/api/v1/opportunities", params={"assessment_status": "SCORED"})
    assert listed.json()["total"] == 0
    listed_all = client.get("/api/v1/opportunities")
    assert listed_all.json()["items"][0]["assessment"]["status"] == "INSUFFICIENT_DATA"
    assert listed_all.json()["items"][0]["assessment"]["ranking_label"] != "HIGH_OPPORTUNITY"

    history = client.get(f"/api/v1/opportunities/{TOKEN_A}/history")
    assert history.json()["items"][0]["status"] == "INSUFFICIENT_DATA"
    assert history.json()["items"][0]["ranking_label"] == "INSUFFICIENT_DATA"

    session = get_session_factory()()
    stored = session.scalar(select(OpportunityAssessment))
    assert stored is not None
    assert stored.status == "SCORED"
    assert stored.ranking_label == "HIGH_OPPORTUNITY"
    assert stored.opportunity_score == 68.6
    session.close()


def test_stale_assessment_is_recomputed_from_stored_snapshots(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    now = utc_now()
    token = Token(
        token_address=TOKEN_A,
        name="Alpha",
        symbol="ALP",
        chain="solana",
        pair_created_at=now - timedelta(hours=12),
        last_discovered_at=now - timedelta(hours=12),
    )
    session.add(token)
    session.commit()
    session.add(
        MarketSnapshot(
            token_id=token.id,
            price_usd=0.01,
            liquidity_usd=18000,
            volume_24h=2000,
            buys_24h=10,
            sells_24h=10,
            recorded_at=now - timedelta(hours=4),
        )
    )
    session.add(
        OpportunityAssessment(
            token_id=token.id,
            assessed_at=now - timedelta(hours=3),
            status="INSUFFICIENT_DATA",
            ranking_label="INSUFFICIENT_DATA",
            opportunity_score=99,
            positive_opportunity_score=99,
            security_adjustment=0,
            confidence=0.2,
            data_completeness=0.3,
            available_components_json="[]",
            missing_components_json='["market_momentum"]',
            component_scores_json="[]",
            positive_signals_json="[]",
            negative_signals_json="[]",
            warnings_json="[]",
            explanation="old single-snapshot assessment",
            snapshot_count=1,
            last_snapshot_at=now - timedelta(hours=4),
        )
    )
    session.add_all(
        [
            MarketSnapshot(
                token_id=token.id,
                price_usd=0.02,
                liquidity_usd=20000,
                volume_24h=4000,
                buys_24h=20,
                sells_24h=12,
                recorded_at=now - timedelta(hours=2),
            ),
            MarketSnapshot(
                token_id=token.id,
                price_usd=0.03,
                liquidity_usd=24000,
                volume_24h=8000,
                buys_24h=40,
                sells_24h=15,
                recorded_at=now - timedelta(hours=1),
            ),
            MarketSnapshot(
                token_id=token.id,
                price_usd=9.99,
                liquidity_usd=1,
                volume_24h=1,
                recorded_at=now + timedelta(hours=3),
            ),
        ]
    )
    session.commit()
    session.close()

    stale_view = client.get("/api/v1/opportunities", params={"include_stale": True})
    assert stale_view.status_code == 200
    stale_item = stale_view.json()["items"][0]
    assert stale_item["snapshot_count"] == 3
    assert stale_item["assessment"]["snapshot_count"] == 1
    assert stale_item["assessment"]["opportunity_score"] == 99
    assert stale_item["freshness"]["is_stale"] is True
    assert stale_item["freshness"]["current_snapshot_count"] == 3
    assert stale_item["freshness"]["assessment_snapshot_count"] == 1
    assert stale_item["freshness"]["stale_reason"]

    current = client.get("/api/v1/opportunities")
    item = current.json()["items"][0]
    assert item["snapshot_count"] == 3
    assert item["assessment"]["snapshot_count"] == 3
    assert item["assessment"]["opportunity_score"] != 99
    assert item["freshness"]["is_stale"] is False
    assert item["freshness"]["source"] == "recomputed_from_stored_snapshots"
    assert item["freshness"]["current_snapshot_count"] == item["assessment"]["snapshot_count"]
    momentum = next(
        component
        for component in item["assessment"]["components"]
        if component["name"] == "market_momentum"
    )
    assert momentum["status"] == "MEASURED"

    history = client.get(f"/api/v1/opportunities/{TOKEN_A}/history")
    assert history.json()["items"][0]["snapshot_count"] == 1
    assert history.json()["items"][0]["opportunity_score"] == 99

    session = get_session_factory()()
    assert session.scalar(select(OpportunityAssessment)).opportunity_score == 99
    session.close()


def test_stale_high_score_does_not_outrank_current_assessment(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    now = utc_now()
    stale_token = Token(
        token_address=TOKEN_A,
        name="Stale",
        symbol="OLD",
        chain="solana",
        pair_created_at=now - timedelta(days=30),
        last_discovered_at=now - timedelta(days=20),
    )
    fresh_token = Token(
        token_address=TOKEN_B,
        name="Fresh",
        symbol="NEW",
        chain="solana",
        pair_created_at=now - timedelta(hours=8),
        last_discovered_at=now,
    )
    session.add_all([stale_token, fresh_token])
    session.commit()
    session.add(
        MarketSnapshot(
            token_id=stale_token.id,
            liquidity_usd=5000,
            volume_24h=100,
            recorded_at=now - timedelta(hours=5),
        )
    )
    session.add(
        OpportunityAssessment(
            token_id=stale_token.id,
            assessed_at=now - timedelta(hours=4),
            status="SCORED",
            ranking_label="HIGH_OPPORTUNITY",
            opportunity_score=99,
            positive_opportunity_score=99,
            security_adjustment=0,
            confidence=0.9,
            data_completeness=0.9,
            available_components_json="[]",
            missing_components_json="[]",
            component_scores_json="[]",
            positive_signals_json="[]",
            negative_signals_json="[]",
            warnings_json="[]",
            explanation="stale high score",
            snapshot_count=1,
            last_snapshot_at=now - timedelta(hours=5),
        )
    )
    session.add(
        MarketSnapshot(
            token_id=stale_token.id,
            liquidity_usd=5200,
            volume_24h=110,
            recorded_at=now - timedelta(minutes=10),
        )
    )
    session.add(
        MarketSnapshot(
            token_id=fresh_token.id,
            price_usd=0.02,
            liquidity_usd=25000,
            volume_24h=8000,
            buys_24h=40,
            sells_24h=10,
            recorded_at=now - timedelta(hours=2),
        )
    )
    session.add(
        MarketSnapshot(
            token_id=fresh_token.id,
            price_usd=0.03,
            liquidity_usd=27000,
            volume_24h=12000,
            buys_24h=50,
            sells_24h=12,
            recorded_at=now,
        )
    )
    session.add(
        OpportunityAssessment(
            token_id=fresh_token.id,
            assessed_at=now,
            status="LOW_CONFIDENCE",
            ranking_label="WATCHLIST",
            opportunity_score=55,
            positive_opportunity_score=55,
            security_adjustment=0,
            confidence=0.4,
            data_completeness=0.55,
            available_components_json='["market_momentum","liquidity_quality","early_discovery"]',
            missing_components_json='["narrative_momentum","security_adjustment"]',
            component_scores_json="[]",
            positive_signals_json="[]",
            negative_signals_json="[]",
            warnings_json="[]",
            explanation="current",
            snapshot_count=2,
            last_snapshot_at=now,
        )
    )
    session.commit()
    session.close()

    ranked = client.get("/api/v1/opportunities")
    items = ranked.json()["items"]
    assert items[0]["token_address"] == TOKEN_B
    assert items[0]["freshness"]["is_stale"] is False
    assert items[0]["assessment"]["opportunity_score"] != 99
    stale_item = next(item for item in items if item["token_address"] == TOKEN_A)
    assert stale_item["assessment"]["opportunity_score"] != 99
    assert stale_item["freshness"]["source"] == "recomputed_from_stored_snapshots"
    assert stale_item["snapshot_count"] == stale_item["assessment"]["snapshot_count"]
