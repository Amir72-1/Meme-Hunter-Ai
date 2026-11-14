"""Market-only historical evaluation: lookahead, gaps, missing fields, and costs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import get_session_factory
from app.models import HistoricalEvaluation, MarketSnapshot, Token
from app.services.historical_evaluation import (
    HORIZON_HOUR,
    HORIZON_NEXT,
    NO_PRIOR,
    PRIOR_UP,
    evaluate_token_snapshots,
    pct_change,
    round_trip_cost,
    run_historical_evaluation,
    valuation_usd,
)

BASE = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _snap(
    token_id: int,
    minutes: float,
    *,
    market_cap: float | None = 1000,
    fdv: float | None = None,
    liquidity: float | None = 5000,
    volume: float | None = 2000,
    snap_id: int | None = None,
) -> MarketSnapshot:
    row = MarketSnapshot(
        token_id=token_id,
        market_cap=market_cap,
        fdv=fdv,
        liquidity_usd=liquidity,
        volume_24h=volume,
        recorded_at=BASE + timedelta(minutes=minutes),
    )
    if snap_id is not None:
        row.id = snap_id
    return row


def test_valuation_prefers_market_cap_and_skips_missing() -> None:
    cap = _snap(1, 0, market_cap=200, fdv=400)
    fdv_only = _snap(1, 0, market_cap=None, fdv=400)
    missing = _snap(1, 0, market_cap=None, fdv=None)
    zero = _snap(1, 0, market_cap=0, fdv=0)
    assert valuation_usd(cap) == 200
    assert valuation_usd(fdv_only) == 400
    assert valuation_usd(missing) is None
    assert valuation_usd(zero) is None


def test_missing_liquidity_is_not_zero_filled() -> None:
    snaps = [
        _snap(1, 0, liquidity=None, market_cap=1000, snap_id=1),
        _snap(1, 15, liquidity=None, market_cap=1100, snap_id=2),
    ]
    outcomes, coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert coverage.outcomes_measured == 1
    assert outcomes[0].gross_return == pct_change(1000, 1100)
    assert outcomes[0].liquidity_change is None
    assert coverage.liquidity_changes_measured == 0
    assert coverage.liquidity_missing_at_signal == 2


def test_missing_market_cap_and_fdv_excludes_signal() -> None:
    snaps = [
        _snap(1, 0, market_cap=None, fdv=None, snap_id=1),
        _snap(1, 15, market_cap=1100, snap_id=2),
    ]
    outcomes, coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert outcomes == []
    assert coverage.excluded_missing_valuation == 1
    assert coverage.outcomes_measured == 0


def test_gap_over_30_minutes_is_missing_not_a_bar() -> None:
    snaps = [
        _snap(1, 0, market_cap=1000, snap_id=1),
        _snap(1, 45, market_cap=1500, snap_id=2),
    ]
    outcomes, coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert outcomes == []
    assert coverage.excluded_gap == 1


def test_insufficient_history_with_one_snapshot() -> None:
    snaps = [_snap(1, 0, snap_id=1)]
    outcomes, coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert outcomes == []
    assert coverage.excluded_insufficient_history == 1


def test_timestamp_ordering_does_not_use_shuffled_input_order() -> None:
    later = _snap(1, 15, market_cap=1200, snap_id=2)
    earlier = _snap(1, 0, market_cap=1000, snap_id=1)
    outcomes, _coverage = evaluate_token_snapshots(
        "Tok",
        [later, earlier],
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert len(outcomes) == 1
    assert outcomes[0].gross_return == pct_change(1000, 1200)
    assert outcomes[0].signal_at < outcomes[0].outcome_at


def test_lookahead_features_ignore_future_snapshots() -> None:
    snaps = [
        _snap(1, 0, market_cap=1000, snap_id=1),
        _snap(1, 15, market_cap=1100, snap_id=2),
        _snap(1, 30, market_cap=99999, snap_id=3),
    ]
    outcomes, _coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    first = outcomes[0]
    assert first.category == NO_PRIOR
    assert first.gross_return == pct_change(1000, 1100)
    assert first.gross_return != pct_change(1000, 99999)
    second = outcomes[1]
    assert second.category == PRIOR_UP
    assert second.gross_return == round(pct_change(1100, 99999) or 0, 6)


def test_one_hour_requires_continuous_path() -> None:
    continuous = [
        _snap(1, 0, market_cap=1000, snap_id=1),
        _snap(1, 15, market_cap=1010, snap_id=2),
        _snap(1, 30, market_cap=1020, snap_id=3),
        _snap(1, 45, market_cap=1030, snap_id=4),
        _snap(1, 60, market_cap=1200, snap_id=5),
    ]
    outcomes, coverage = evaluate_token_snapshots(
        "Tok",
        continuous,
        horizon=HORIZON_HOUR,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert coverage.outcomes_measured == 1
    assert outcomes[0].gross_return == pct_change(1000, 1200)
    assert outcomes[0].mfe == pct_change(1000, 1200)
    assert outcomes[0].elapsed_minutes == 60

    gapped = [
        _snap(1, 0, market_cap=1000, snap_id=1),
        _snap(1, 15, market_cap=1010, snap_id=2),
        _snap(1, 60, market_cap=1500, snap_id=3),
    ]
    gapped_out, gapped_cov = evaluate_token_snapshots(
        "Tok",
        gapped,
        horizon=HORIZON_HOUR,
        fee_bps=0,
        slippage_bps=0,
        max_gap_minutes=30,
    )
    assert gapped_out == []
    assert gapped_cov.excluded_gap >= 1


def test_fee_and_slippage_are_round_trip_and_not_fills() -> None:
    cost = round_trip_cost(30, 50)
    assert abs(cost - 0.016) < 1e-12
    snaps = [
        _snap(1, 0, market_cap=1000, snap_id=1),
        _snap(1, 15, market_cap=1100, snap_id=2),
    ]
    outcomes, _coverage = evaluate_token_snapshots(
        "Tok",
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=30,
        slippage_bps=50,
        max_gap_minutes=30,
    )
    gross = pct_change(1000, 1100)
    assert outcomes[0].gross_return == gross
    assert outcomes[0].net_return == round(gross - cost, 6)


def test_run_persists_summary_without_rewriting_snapshots(client: TestClient) -> None:
    session = get_session_factory()()
    before = session.scalar(select(MarketSnapshot).order_by(MarketSnapshot.id).limit(1))
    before_count = len(list(session.scalars(select(MarketSnapshot)).all()))
    token = Token(token_address="HistEvalTok11111111111111111111111111111", chain="solana")
    session.add(token)
    session.flush()
    session.add_all(
        [
            _snap(token.id, 0, market_cap=1000),
            _snap(token.id, 15, market_cap=1300),
        ]
    )
    session.commit()
    result = run_historical_evaluation(
        session,
        get_settings(),
        horizon=HORIZON_NEXT,
        token_address=token.token_address,
    )
    assert result.sample_size == 1
    assert result.evaluation_id is not None
    stored = session.get(HistoricalEvaluation, result.evaluation_id)
    assert stored is not None
    assert stored.horizon == HORIZON_NEXT
    after_count = len(list(session.scalars(select(MarketSnapshot)).all()))
    assert after_count == before_count + 2
    if before is not None:
        still = session.get(MarketSnapshot, before.id)
        assert still is not None
        assert still.recorded_at == before.recorded_at
    session.close()


def test_historical_api_evaluate_and_fetch(client: TestClient) -> None:
    session = get_session_factory()()
    token = Token(token_address="HistApiTok111111111111111111111111111111", chain="solana")
    session.add(token)
    session.flush()
    session.add_all(
        [
            _snap(token.id, 0, market_cap=800),
            _snap(token.id, 15, market_cap=900),
        ]
    )
    session.commit()
    session.close()

    bad = client.post("/api/v1/historical/evaluate", json={"horizon": "five_minutes"})
    assert bad.status_code == 422

    response = client.post(
        "/api/v1/historical/evaluate",
        json={"horizon": "next_observation", "token_address": token.token_address, "fee_bps": 0, "slippage_bps": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["horizon"] == "next_observation"
    assert body["sample_size"] == 1
    assert body["paper_trading_only"] is True
    assert "wallet" not in body["explanation"].lower()
    evaluation_id = body["evaluation_id"]

    fetched = client.get(f"/api/v1/historical/evaluations/{evaluation_id}")
    assert fetched.status_code == 200
    assert fetched.json()["evaluation_id"] == evaluation_id

    listing = client.get("/api/v1/historical/evaluations")
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    status_payload = client.get("/api/v1/historical/status")
    assert status_payload.status_code == 200
    assert status_payload.json()["last_status"] == "completed"

    missing = client.get("/api/v1/historical/evaluations/999999")
    assert missing.status_code == 404
