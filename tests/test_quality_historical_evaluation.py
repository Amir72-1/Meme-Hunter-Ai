"""Time-specific quality-aware evaluation. Does not mutate snapshots or evaluations 1–2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import HistoricalEvaluation, MarketSnapshot, QualityHistoricalEvaluation, Token
from app.services.historical_evaluation import HORIZON_HOUR, HORIZON_NEXT, evaluate_token_snapshots, round_trip_cost
from app.services.market_data_quality import (
    CLEAN,
    DUPLICATE_VALUATION,
    EXTREME_PRICE_MOVEMENT,
    EXTREME_REVERSAL,
    MISSING_LIQUIDITY,
    PAIR_CHANGED,
    SUSPICIOUS,
    VALUATION_SOURCE_CHANGED,
    WARNING,
)
from app.services.quality_historical_evaluation import (
    ALL_OBSERVED,
    COMPLETE_LIQUIDITY,
    QUALITY_FILTERED,
    SAME_PAIR,
    SAME_PAIR_AND_VALUATION,
    SUSPICIOUS_OR_EXCLUDED,
    assess_interval_quality,
    evaluate_token_quality_outcomes,
    run_quality_historical_evaluation,
)

BASE = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _snap(
    minutes: float,
    *,
    market_cap: float | None = 1000,
    fdv: float | None = 1000,
    price: float | None = 0.001,
    liquidity: float | None = 5000,
    pair: str | None = "PairA",
    snap_id: int | None = None,
    token_id: int = 1,
) -> MarketSnapshot:
    row = MarketSnapshot(
        token_id=token_id,
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


def test_no_look_ahead_bias_on_later_reversal() -> None:
    settings = get_settings()
    t0 = _snap(0, market_cap=1000, price=0.001, liquidity=5000, snap_id=1)
    t1 = _snap(15, market_cap=20000, price=0.02, liquidity=5200, snap_id=2)
    t2 = _snap(30, market_cap=150, price=0.00015, liquidity=5100, snap_id=3)
    spike_flags, spike_class, _ = assess_interval_quality(t0, t1, [t0, t1], None, settings)
    spike_codes = {item.code for item in spike_flags}
    assert EXTREME_PRICE_MOVEMENT in spike_codes
    assert EXTREME_REVERSAL not in spike_codes
    assert spike_class == WARNING

    crash_flags, crash_class, _ = assess_interval_quality(t1, t2, [t1, t2], t0, settings)
    crash_codes = {item.code for item in crash_flags}
    assert EXTREME_REVERSAL in crash_codes
    assert crash_class == SUSPICIOUS


def test_pair_change_and_valuation_source_change() -> None:
    settings = get_settings()
    start = _snap(0, market_cap=1000, fdv=None, pair="PairA", snap_id=1)
    end = _snap(15, market_cap=None, fdv=1100, pair="PairB", snap_id=2)
    flags, _, reasons = assess_interval_quality(start, end, [start, end], None, settings)
    codes = {item.code for item in flags}
    assert PAIR_CHANGED in codes
    assert VALUATION_SOURCE_CHANGED in codes
    assert PAIR_CHANGED in reasons


def test_missing_liquidity_and_missing_valuation() -> None:
    settings = get_settings()
    start = _snap(0, market_cap=1000, liquidity=None, snap_id=1)
    end = _snap(15, market_cap=None, fdv=None, liquidity=None, snap_id=2)
    flags, _, reasons = assess_interval_quality(start, end, [start, end], None, settings)
    codes = {item.code for item in flags}
    assert MISSING_LIQUIDITY in codes
    assert "MISSING_VALUATION" in codes
    assert MISSING_LIQUIDITY in reasons


def test_duplicate_valuation() -> None:
    settings = get_settings()
    start = _snap(0, market_cap=1000, price=0.001, snap_id=1)
    end = _snap(15, market_cap=1000, price=0.001, snap_id=2)
    flags, classification, reasons = assess_interval_quality(start, end, [start, end], None, settings)
    assert DUPLICATE_VALUATION in {item.code for item in flags}
    assert classification == WARNING
    assert DUPLICATE_VALUATION in reasons


def test_stale_gap_on_interval() -> None:
    settings = get_settings()
    start = _snap(0, snap_id=1)
    end = MarketSnapshot(
        token_id=1,
        market_cap=1010,
        fdv=1010,
        price_usd=0.00101,
        liquidity_usd=5050,
        pair_address="PairA",
        recorded_at=BASE + timedelta(hours=7),
    )
    end.id = 2
    flags, _, reasons = assess_interval_quality(start, end, [start, end], None, settings)
    assert "STALE_SNAPSHOT" in {item.code for item in flags}
    assert "STALE_SNAPSHOT" in reasons


def test_cohorts_denominators_and_net_positive() -> None:
    token = Token(id=7, token_address="QualityHistTok11111111111111111111111", chain="solana", symbol="QHT")
    snaps = [
        _snap(0, market_cap=1000, price=0.001, liquidity=5000, snap_id=1, token_id=7),
        _snap(15, market_cap=1020, price=0.00102, liquidity=5050, snap_id=2, token_id=7),
        _snap(30, market_cap=None, fdv=1040, price=0.00104, liquidity=None, pair="PairB", snap_id=3, token_id=7),
    ]
    rows, coverage = evaluate_token_quality_outcomes(
        token,
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=30,
        slippage_bps=50,
        max_gap_minutes=30,
        settings=get_settings(),
    )
    assert coverage["outcomes_measured"] == 2
    cost = round_trip_cost(30, 50)
    assert cost == 0.016
    first, second = rows
    assert first.in_quality_filtered is True
    assert first.classification == CLEAN
    assert first.gross_return == 0.02
    assert first.net_return == round(0.02 - 0.016, 6)
    assert first.gross_return > 0.016
    assert second.in_quality_filtered is False
    assert second.in_same_pair is False
    assert second.in_complete_liquidity is False
    assert first.in_same_pair_and_valuation is True
    original, _ = evaluate_token_snapshots(
        token.token_address,
        snaps,
        horizon=HORIZON_NEXT,
        fee_bps=30,
        slippage_bps=50,
        max_gap_minutes=30,
    )
    assert len(original) == len(rows)


def test_overlapping_one_hour_windows_are_marked_not_dropped() -> None:
    token = Token(id=8, token_address="OverlapTok111111111111111111111111111", chain="solana", symbol="OVL")
    snaps = [
        _snap(minutes, market_cap=1000 + minutes, price=0.001, liquidity=4000, snap_id=i + 1, token_id=8)
        for i, minutes in enumerate(range(0, 135, 15))
    ]
    rows, coverage = evaluate_token_quality_outcomes(
        token,
        snaps,
        horizon=HORIZON_HOUR,
        fee_bps=30,
        slippage_bps=50,
        max_gap_minutes=30,
        settings=get_settings(),
    )
    from app.services.quality_historical_evaluation import mark_overlapping_windows

    mark_overlapping_windows(rows, HORIZON_HOUR)
    assert coverage["outcomes_measured"] == len(rows)
    assert len(rows) >= 2
    overlapping = sum(1 for item in rows if item.overlaps_prior_window)
    kept = sum(1 for item in rows if not item.overlaps_prior_window)
    assert overlapping + kept == len(rows)
    assert overlapping > 0
    assert kept >= 1


def test_scan_does_not_mutate_snapshots_or_original_evaluations(client: TestClient) -> None:
    session = get_session_factory()()
    session.add(
        HistoricalEvaluation(
            started_at=BASE,
            finished_at=BASE,
            status="completed",
            horizon="next_observation",
            fee_bps=30,
            slippage_bps=50,
            tokens_considered=1,
            signals_considered=2,
            outcomes_measured=14040,
            explanation="preserved fixture",
        )
    )
    session.add(
        HistoricalEvaluation(
            started_at=BASE + timedelta(minutes=1),
            finished_at=BASE + timedelta(minutes=1),
            status="completed",
            horizon="one_hour",
            fee_bps=30,
            slippage_bps=50,
            tokens_considered=1,
            signals_considered=2,
            outcomes_measured=10759,
            explanation="preserved fixture",
        )
    )
    token = Token(token_address="PreserveQHist111111111111111111111111", chain="solana", symbol="PQH")
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
                pair_address="PairP",
                recorded_at=BASE,
            ),
            MarketSnapshot(
                token_id=token.id,
                market_cap=1016,
                fdv=1016,
                price_usd=0.001016,
                liquidity_usd=4010,
                pair_address="PairP",
                recorded_at=BASE + timedelta(minutes=15),
            ),
        ]
    )
    session.commit()
    snap_count = session.scalar(select(func.count()).select_from(MarketSnapshot))
    eval_rows = list(session.scalars(select(HistoricalEvaluation).order_by(HistoricalEvaluation.id)).all())
    assert [row.outcomes_measured for row in eval_rows[-2:]] == [14040, 10759]
    payload = run_quality_historical_evaluation(
        session,
        get_settings(),
        horizon=HORIZON_NEXT,
        token_address=token.token_address,
    )
    assert payload["status"] == "completed"
    assert payload["cohorts"][ALL_OBSERVED]["sample_size"] == 1
    assert payload["cohorts"][QUALITY_FILTERED]["sample_size"] == 1
    assert payload["cohorts"][COMPLETE_LIQUIDITY]["sample_size"] == 1
    assert payload["cohorts"][SAME_PAIR]["sample_size"] == 1
    assert payload["cohorts"][SAME_PAIR_AND_VALUATION]["sample_size"] == 1
    assert payload["cohorts"][SUSPICIOUS_OR_EXCLUDED]["sample_size"] == 0
    after_snaps = session.scalar(select(func.count()).select_from(MarketSnapshot))
    after_evals = list(session.scalars(select(HistoricalEvaluation).order_by(HistoricalEvaluation.id)).all())
    assert after_snaps == snap_count
    assert [row.outcomes_measured for row in after_evals[-2:]] == [14040, 10759]
    quality_runs = session.scalar(select(func.count()).select_from(QualityHistoricalEvaluation))
    assert quality_runs >= 1
    session.close()

    scanned = client.post(
        "/api/v1/historical/quality-evaluate?include_outcomes=true",
        json={"horizon": "next_observation", "token_address": token.token_address},
    )
    assert scanned.status_code == 200
    body = scanned.json()
    assert body["outcomes_measured"] >= 1
    assert ALL_OBSERVED in body["cohorts"]
    assert body["cohorts"][ALL_OBSERVED]["net_positive_pct"] is not None
    status_payload = client.get("/api/v1/historical/quality-status")
    assert status_payload.status_code == 200
    listed = client.get("/api/v1/historical/quality-evaluations")
    assert listed.status_code == 200
    eval_id = body["evaluation_id"]
    fetched = client.get(f"/api/v1/historical/quality-evaluations/{eval_id}")
    assert fetched.status_code == 200
    original_status = client.get("/api/v1/historical/status")
    assert original_status.status_code == 200
