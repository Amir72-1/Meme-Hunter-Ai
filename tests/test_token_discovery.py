"""Discovery pipeline tests: dedupe, upserts, snapshots, filters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, delete

from app.config import get_settings
from app.database import get_session_factory
from app.models import DiscoveryScan, MarketSnapshot, OpportunityAssessment, Token
from app.services.market_data import classify_thresholds, extract_solana_addresses
from app.services.opportunity_scoring import analyze_market_history, score_market_momentum
from app.services.token_discovery import run_discovery_scan
from tests.dexscreener_fakes import (
    TOKEN_A,
    TOKEN_B,
    boost,
    pair_payload,
    profile,
    run,
    ScriptedDex,
)


@pytest.fixture(autouse=True)
def _clean_discovery_tables() -> None:
    session = get_session_factory()()
    session.execute(delete(MarketSnapshot))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()
    yield
    session = get_session_factory()()
    session.execute(delete(MarketSnapshot))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(DiscoveryScan))
    session.commit()
    session.close()


def test_duplicate_token_handling() -> None:
    discovered = extract_solana_addresses(
        [profile(TOKEN_A), profile(TOKEN_A)],
        [boost(TOKEN_A, amount=3)],
        [boost(TOKEN_A, amount=8)],
    )
    assert list(discovered) == [TOKEN_A]
    assert discovered[TOKEN_A].sources == {"profile", "boost_latest", "boost_top"}
    assert discovered[TOKEN_A].active_boosts == 8


def test_database_upserts_and_snapshot_persistence() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.latest_boosts = [boost(TOKEN_A)]
    script.top_boosts = []
    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.02")]

    session = get_session_factory()()

    async def first() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.status == "completed"
        assert result.tokens_processed == 1
        assert result.snapshots_saved == 1

    run(first())

    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.04")]

    async def second() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.snapshots_saved == 1

    run(second())

    token_count = session.scalar(select(func.count()).select_from(Token))
    snapshot_count = session.scalar(select(func.count()).select_from(MarketSnapshot))
    token = session.scalar(select(Token).where(Token.token_address == TOKEN_A))
    assert token_count == 1
    assert snapshot_count == 2
    assert token is not None
    assert "profile" in (token.discovery_source or "")
    assert "boost_latest" in (token.discovery_source or "")
    session.close()


def test_below_threshold_tokens_are_saved() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.latest_boosts = []
    script.top_boosts = []
    script.tokens = [
        pair_payload(
            TOKEN_A,
            liquidity={"usd": 100},
            volume={"h24": 10},
            txns={"h24": {"buys": 1, "sells": 0}},
        )
    ]
    session = get_session_factory()()

    async def body() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.tokens_processed == 1
        assert result.below_threshold_count == 1

    run(body())
    token = session.scalar(select(Token).where(Token.token_address == TOKEN_A))
    assert token is not None
    assert token.filter_status == "below_threshold"
    session.close()


def test_missing_pair_uses_token_pairs_endpoint() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_B)]
    script.latest_boosts = []
    script.top_boosts = []
    script.tokens = []
    script.token_pairs[TOKEN_B] = [pair_payload(TOKEN_B, dexId="orca")]
    session = get_session_factory()()

    async def body() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.tokens_processed == 1

    run(body())
    token = session.scalar(select(Token).where(Token.token_address == TOKEN_B))
    assert token is not None
    assert token.dex_name == "orca"
    session.close()


def test_feed_failure_marks_scan_failed() -> None:
    script = ScriptedDex()
    script.profiles = httpx.Response(503, json={"error": "down"})
    session = get_session_factory()()

    async def body() -> None:
        async with script.client() as client:
            result = await run_discovery_scan(session, client)
        assert result.status == "failed"
        assert result.tokens_processed == 0
        assert result.errors

    run(body())
    session.close()


def test_successive_scans_append_history_for_tokens_that_leave_the_feed() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.latest_boosts = []
    script.top_boosts = []
    script.tokens = [pair_payload(TOKEN_A, priceUsd="0.02", volume={"h24": 4000.0})]
    session = get_session_factory()()

    async def scan() -> None:
        async with script.client() as client:
            await run_discovery_scan(session, client)

    run(scan())
    first = list(session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.id)).all())
    assert len(first) == 1
    first_price = first[0].price_usd
    first_recorded = first[0].recorded_at

    script.profiles = [profile(TOKEN_B)]
    script.tokens = [
        pair_payload(TOKEN_A, priceUsd="0.03", volume={"h24": 8000.0}),
        pair_payload(TOKEN_B, priceUsd="0.01", volume={"h24": 5000.0}),
    ]
    run(scan())
    token_a = session.scalar(select(Token).where(Token.token_address == TOKEN_A))
    assert token_a is not None
    two = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token_a.id)
            .order_by(MarketSnapshot.recorded_at.asc(), MarketSnapshot.id.asc())
        ).all()
    )
    assert len(two) == 2
    assert two[0].price_usd == first_price
    assert two[0].recorded_at == first_recorded
    assert two[1].price_usd == 0.03
    assert two[1].recorded_at >= two[0].recorded_at

    script.tokens = [
        pair_payload(TOKEN_A, priceUsd="0.04", volume={"h24": 16000.0}),
        pair_payload(TOKEN_B, priceUsd="0.011", volume={"h24": 6000.0}),
    ]
    run(scan())
    three = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token_a.id)
            .order_by(MarketSnapshot.recorded_at.asc(), MarketSnapshot.id.asc())
        ).all()
    )
    assert len(three) == 3
    assert three[0].price_usd == 0.02
    assert [row.id for row in three] == sorted(row.id for row in three)
    history = analyze_market_history(three, datetime.now(timezone.utc), 6.0)
    assert history["snapshot_count"] == 3
    assert history["volume_change"]["status"] == "MEASURED"
    assert history["acceleration"]["status"] == "MEASURED"
    momentum = score_market_momentum(three, get_settings(), datetime.now(timezone.utc), 0.25)
    assert momentum.status == "MEASURED"
    assert momentum.score is not None
    session.close()


def test_zero_previous_volume_from_discovery_snapshots_is_safe() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.latest_boosts = []
    script.top_boosts = []
    script.tokens = [pair_payload(TOKEN_A, volume={"h24": 0.0}, priceUsd="0.01")]
    session = get_session_factory()()

    async def scan() -> None:
        async with script.client() as client:
            await run_discovery_scan(session, client)

    run(scan())
    script.tokens = [pair_payload(TOKEN_A, volume={"h24": 9000.0}, priceUsd="0.02")]
    run(scan())
    snaps = list(session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.id)).all())
    history = analyze_market_history(snaps, datetime.now(timezone.utc), 6.0)
    assert history["volume_change"]["status"] == "UNAVAILABLE"
    momentum = score_market_momentum(snaps, get_settings(), datetime.now(timezone.utc), 0.25)
    assert momentum.status == "MEASURED"
    session.close()


def test_threshold_classification_uses_config() -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    status, reasons = classify_thresholds(
        liquidity_usd=50000,
        volume_24h=20000,
        pair_created_at=now - timedelta(hours=2),
        txns_24h=80,
        settings=settings,
        now=now,
    )
    assert status == "meets_threshold"
    assert reasons == ""
