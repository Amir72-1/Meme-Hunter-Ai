"""Stored-snapshot data-quality flags. Does not mutate market history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import HistoricalEvaluation, MarketSnapshot, Token
from app.services.market_data_quality import (
    CLEAN,
    DUPLICATE_VALUATION,
    EXTREME_PRICE_MOVEMENT,
    EXTREME_REVERSAL,
    INSUFFICIENT_DATA,
    INSUFFICIENT_HISTORY,
    LIQUIDITY_DISLOCATION,
    MISSING_LIQUIDITY,
    MISSING_VALUATION,
    PAIR_CHANGED,
    POSSIBLE_QUOTE_REGIME_CHANGE,
    STALE_SNAPSHOT,
    SUSPICIOUS,
    VALUATION_SOURCE_CHANGED,
    WARNING,
    analyze_token_snapshots,
    run_quality_scan,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
BASE = NOW - timedelta(hours=2)


def _snap(
    minutes: float,
    *,
    market_cap: float | None = 1000,
    fdv: float | None = 1000,
    price: float | None = 0.001,
    liquidity: float | None = 5000,
    pair: str | None = "PairAAA",
    snap_id: int | None = None,
) -> MarketSnapshot:
    row = MarketSnapshot(
        token_id=1,
        market_cap=market_cap,
        fdv=fdv,
        price_usd=price,
        liquidity_usd=liquidity,
        pair_address=pair,
        recorded_at=BASE + timedelta(minutes=minutes),
    )
    if snap_id is not None:
        row.id = snap_id
    return row


def _analyze(snaps: list[MarketSnapshot]):
    return analyze_token_snapshots("Tok", snaps, get_settings(), now=NOW)


def test_missing_liquidity_is_flagged_not_zeroed() -> None:
    result = _analyze(
        [
            _snap(0, liquidity=None, snap_id=1),
            _snap(15, liquidity=None, snap_id=2),
        ]
    )
    assert MISSING_LIQUIDITY in result.flag_codes()
    assert result.liquidity.missing_liquidity == 2
    assert result.liquidity.non_null_liquidity == 0
    assert result.liquidity.missing_pct == 1.0
    assert "zero" in result.liquidity.as_dict()["note"].lower()


def test_missing_market_cap_and_fdv() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=None, fdv=None, snap_id=1),
            _snap(15, market_cap=None, fdv=None, snap_id=2),
        ]
    )
    assert MISSING_VALUATION in result.flag_codes()


def test_pair_change_is_flagged() -> None:
    result = _analyze(
        [
            _snap(0, pair="PairA", snap_id=1),
            _snap(15, pair="PairB", snap_id=2),
        ]
    )
    assert PAIR_CHANGED in result.flag_codes()
    assert result.pair_changes == 1


def test_valuation_source_change() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=1000, fdv=None, snap_id=1),
            _snap(15, market_cap=None, fdv=2000, snap_id=2),
        ]
    )
    assert VALUATION_SOURCE_CHANGED in result.flag_codes()


def test_extreme_gain() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=1000, price=0.001, liquidity=2000, snap_id=1),
            _snap(15, market_cap=15000, price=0.015, liquidity=3000, snap_id=2),
        ]
    )
    assert EXTREME_PRICE_MOVEMENT in result.flag_codes()
    assert result.classification in {WARNING, SUSPICIOUS}


def test_extreme_reversal_and_possible_regime_change() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=1000, price=0.001, liquidity=2000, snap_id=1),
            _snap(15, market_cap=20000, price=0.02, liquidity=80000, snap_id=2),
            _snap(30, market_cap=1500, price=0.0015, liquidity=2500, snap_id=3),
        ]
    )
    assert EXTREME_REVERSAL in result.flag_codes()
    assert POSSIBLE_QUOTE_REGIME_CHANGE in result.flag_codes()
    assert LIQUIDITY_DISLOCATION in result.flag_codes()
    assert result.classification == SUSPICIOUS


def test_duplicate_valuation() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=1000, price=0.001, snap_id=1),
            _snap(15, market_cap=1000, price=0.001, snap_id=2),
        ]
    )
    assert DUPLICATE_VALUATION in result.flag_codes()


def test_stale_snapshot() -> None:
    old = [
        MarketSnapshot(
            token_id=1,
            market_cap=1000,
            fdv=1000,
            price_usd=0.001,
            liquidity_usd=5000,
            pair_address="PairAAA",
            recorded_at=NOW - timedelta(hours=10),
        ),
        MarketSnapshot(
            token_id=1,
            market_cap=1010,
            fdv=1010,
            price_usd=0.00101,
            liquidity_usd=5100,
            pair_address="PairAAA",
            recorded_at=NOW - timedelta(hours=9, minutes=45),
        ),
    ]
    old[0].id = 1
    old[1].id = 2
    result = analyze_token_snapshots("Tok", old, get_settings(), now=NOW)
    assert STALE_SNAPSHOT in result.flag_codes()


def test_insufficient_history() -> None:
    result = _analyze([_snap(0, snap_id=1)])
    assert INSUFFICIENT_HISTORY in result.flag_codes()
    assert result.classification == INSUFFICIENT_DATA


def test_clean_classification_is_not_a_safety_rating() -> None:
    result = _analyze(
        [
            _snap(0, market_cap=1000, price=0.001, liquidity=5000, snap_id=1),
            _snap(15, market_cap=1010, price=0.00101, liquidity=5050, snap_id=2),
            _snap(30, market_cap=1020, price=0.00102, liquidity=5100, snap_id=3),
        ]
    )
    assert result.classification == CLEAN
    assert "not a safety rating" in result.explanation.lower()
    assert not result.flag_codes()


def test_scan_does_not_mutate_snapshots_or_evaluations(client: TestClient) -> None:
    session = get_session_factory()()
    snap_count = session.scalar(select(func.count()).select_from(MarketSnapshot))
    eval_count = session.scalar(select(func.count()).select_from(HistoricalEvaluation))
    token = Token(token_address="QualityTok11111111111111111111111111111", chain="solana", symbol="QTOK")
    session.add(token)
    session.flush()
    session.add_all(
        [
            MarketSnapshot(
                token_id=token.id,
                market_cap=1000,
                fdv=1000,
                price_usd=0.001,
                liquidity_usd=4000,
                pair_address="PairQ",
                recorded_at=BASE,
            ),
            MarketSnapshot(
                token_id=token.id,
                market_cap=1015,
                fdv=1015,
                price_usd=0.001015,
                liquidity_usd=4010,
                pair_address="PairQ",
                recorded_at=BASE + timedelta(minutes=15),
            ),
        ]
    )
    session.commit()
    payload = run_quality_scan(session, get_settings(), token_address=token.token_address, now=NOW)
    assert payload["status"] == "completed"
    assert payload["scan_id"] is not None
    after_snaps = session.scalar(select(func.count()).select_from(MarketSnapshot))
    after_evals = session.scalar(select(func.count()).select_from(HistoricalEvaluation))
    assert after_snaps == snap_count + 2
    assert after_evals == eval_count
    session.close()

    scanned = client.post("/api/v1/data-quality/scan", json={"token_address": token.token_address})
    assert scanned.status_code == 200
    body = scanned.json()
    assert body["tokens_scanned"] >= 1
    assert "missing_pct" in body["liquidity"]

    status_payload = client.get("/api/v1/data-quality/status")
    assert status_payload.status_code == 200
    assert status_payload.json()["last_status"] == "completed"

    summary = client.get("/api/v1/data-quality/summary")
    assert summary.status_code == 200

    token_view = client.get(f"/api/v1/data-quality/tokens/{token.token_address}")
    assert token_view.status_code == 200
    assert token_view.json()["classification"] in {CLEAN, WARNING, SUSPICIOUS, INSUFFICIENT_DATA}

    missing = client.get("/api/v1/data-quality/tokens/DoesNotExist111")
    assert missing.status_code == 404
