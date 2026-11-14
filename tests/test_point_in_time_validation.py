"""Point-in-time validation: no future leakage, null liquidity, frozen thresholds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    HistoricalEvaluation,
    MarketSnapshot,
    Narrative,
    OpportunityAssessment,
    QualityHistoricalEvaluation,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, SCORED
from app.services.point_in_time_validation import (
    reconstruct_opportunity,
    E1,
    E4,
    E5,
    G0,
    G1,
    G2,
    G3,
    MEASURED,
    MISSING_OUTCOME,
    OVERLAP_1H,
    OVERLAP_BASELINE,
    PURGE_MINUTES,
    SPLIT_HOLDOUT,
    SPLIT_PURGE,
    SPLIT_RESEARCH,
    apply_overlap,
    assign_split,
    classify_entry,
    feature_history,
    future_snapshots,
    ordered_snapshots,
    pair_outcome,
    run_point_in_time_evaluation,
)
from app.services.quality_historical_evaluation import QUALITY_FILTERED

BASE = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)


def _snap(
    minutes: float,
    *,
    market_cap: float | None = 1000,
    price: float | None = 0.001,
    liquidity: float | None = 5000,
    pair: str | None = "PairA",
    snap_id: int | None = None,
    token_id: int = 1,
    recorded_at: datetime | None = None,
) -> MarketSnapshot:
    row = MarketSnapshot(
        token_id=token_id,
        market_cap=market_cap,
        fdv=market_cap,
        price_usd=price,
        liquidity_usd=liquidity,
        pair_address=pair,
        recorded_at=recorded_at or (BASE + timedelta(minutes=minutes)),
    )
    if snap_id is not None:
        row.id = snap_id
    return row


def test_last_snapshot_without_future_is_g0_candidate() -> None:
    settings = get_settings()
    snaps = [_snap(0, snap_id=1), _snap(15, snap_id=2)]
    ordered = ordered_snapshots(snaps)
    last = ordered[-1]
    future = future_snapshots(ordered, last)
    assert future == []
    history = feature_history(ordered, last)
    g1, g2, g3, reasons = classify_entry(
        last, history, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA"
    )
    assert last in history
    assert MEASURED not in reasons
    paired = pair_outcome(last, future, horizon="next_observation", max_gap_minutes=30, fee_bps=30, slippage_bps=50, settings=settings)
    assert paired["coverage_status"] == MISSING_OUTCOME
    assert paired["gross_return"] is None
    assert g1 is True
    assert g3 is False


def test_future_outcome_does_not_affect_entry() -> None:
    settings = get_settings()
    t0 = _snap(0, snap_id=1, liquidity=None)
    t1 = _snap(15, snap_id=2, liquidity=None, market_cap=1100)
    history = feature_history([t0, t1], t0)
    g1_a, _, _, _ = classify_entry(t0, history, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA")
    history_with_future = feature_history([t0, t1], t0)
    g1_b, _, _, _ = classify_entry(t0, history_with_future, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA")
    assert g1_a is False
    assert g1_a == g1_b
    assert E1 in classify_entry(t0, history, settings, opportunity_status=None, ranking_label=None)[3]


def test_same_timestamp_peers_do_not_leak_into_features_or_outcomes() -> None:
    a = _snap(0, snap_id=10, market_cap=1000)
    b = _snap(0, snap_id=11, market_cap=999999, pair="OtherPair")
    later = _snap(15, snap_id=12, market_cap=1010)
    ordered = ordered_snapshots([a, b, later])
    hist_a = feature_history(ordered, a)
    assert b not in hist_a
    assert later not in hist_a
    fut_a = future_snapshots(ordered, a)
    assert b not in fut_a
    assert later in fut_a
    paired = pair_outcome(
        a, fut_a, horizon="next_observation", max_gap_minutes=30, fee_bps=30, slippage_bps=50, settings=get_settings()
    )
    assert paired["outcome_snapshot_id"] == later.id
    assert paired["coverage_status"] == MEASURED


def test_future_pair_change_does_not_invalidate_earlier_entry() -> None:
    settings = get_settings()
    t0 = _snap(0, snap_id=1, pair="PairA")
    t1 = _snap(15, snap_id=2, pair="PairA", market_cap=1010)
    t2 = _snap(30, snap_id=3, pair="PairB", market_cap=1020)
    history = feature_history([t0, t1, t2], t1)
    g1, _, _, reasons = classify_entry(
        t1, history, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA"
    )
    assert g1 is True
    assert E4 not in reasons


def test_future_reversal_does_not_invalidate_earlier_entry() -> None:
    settings = get_settings()
    t0 = _snap(0, snap_id=1, market_cap=1000, liquidity=5000)
    t1 = _snap(15, snap_id=2, market_cap=20000, liquidity=5200, price=0.02)
    t2 = _snap(30, snap_id=3, market_cap=150, liquidity=5100, price=0.00015)
    history = feature_history([t0, t1, t2], t1)
    g1, _, _, reasons = classify_entry(
        t1, history, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA"
    )
    assert E5 not in reasons
    later_history = feature_history([t0, t1, t2], t2)
    _, _, _, later_reasons = classify_entry(
        t2, later_history, settings, opportunity_status="INSUFFICIENT_DATA", ranking_label="INSUFFICIENT_DATA"
    )
    assert E5 in later_reasons


def test_null_liquidity_g1_allows_g2_excludes() -> None:
    settings = get_settings()
    t0 = _snap(0, snap_id=1, liquidity=None)
    t1 = _snap(15, snap_id=2, liquidity=None, market_cap=1010)
    g1, g2, g3, _ = classify_entry(
        t1,
        feature_history([t0, t1], t1),
        settings,
        opportunity_status=SCORED,
        ranking_label=HIGH_OPPORTUNITY,
    )
    assert g1 is True
    assert g2 is False
    assert g3 is False
    assert t1.liquidity_usd is None
    rich = _snap(15, snap_id=3, liquidity=12000, market_cap=1010)
    g1b, g2b, g3b, _ = classify_entry(
        rich,
        feature_history([t0, rich], rich),
        settings,
        opportunity_status=SCORED,
        ranking_label=HIGH_OPPORTUNITY,
    )
    assert g1b and g2b and g3b


def test_g3_keeps_frozen_thresholds() -> None:
    settings = get_settings()
    assert settings.opportunity_high_threshold == 70.0
    assert settings.opportunity_min_confidence == 0.50
    assert settings.opportunity_min_completeness == 0.40
    t0 = _snap(0, snap_id=1, liquidity=20000)
    t1 = _snap(15, snap_id=2, liquidity=20000, market_cap=1010)
    history = feature_history([t0, t1], t1)
    _, g2, g3, _ = classify_entry(t1, history, settings, opportunity_status=SCORED, ranking_label="WATCHLIST")
    assert g2 is True
    assert g3 is False


def test_later_security_and_narratives_are_invisible_at_t() -> None:
    settings = get_settings()
    token = Token(id=9, token_address="AsOfTok", chain="solana", symbol="A")
    t0 = _snap(0, snap_id=1, token_id=9)
    t1 = _snap(15, snap_id=2, token_id=9, market_cap=1010)
    history = feature_history([t0, t1], t1)
    future_risk = TokenRiskAssessment(
        token_id=9,
        scanned_at=BASE + timedelta(days=2),
        status="scored",
        risk_score=90,
        explanation="future",
    )
    narrative = Narrative(
        id=1,
        name="FutureNarr",
        last_detected_at=BASE + timedelta(days=2),
        first_detected_at=BASE + timedelta(days=2),
        explanation="future",
    )
    match = TokenNarrativeMatch(
        token_id=9,
        narrative_id=1,
        explanation="future match",
        confidence=0.9,
        method="symbol_and_source",
        created_at=BASE + timedelta(days=2),
    )
    result = reconstruct_opportunity(
        settings,
        token,
        history,
        [(match, narrative)],
        [future_risk],
        [],
        BASE + timedelta(minutes=15),
    )
    assert result.security_coverage == "UNAVAILABLE"
    assert result.narrative_match_count == 0


def test_observed_at_after_t_is_excluded_even_if_scanned_at_is_earlier() -> None:
    settings = get_settings()
    token = Token(id=10, token_address="ObsTok", chain="solana", symbol="O")
    t0 = _snap(0, snap_id=1, token_id=10)
    t1 = _snap(15, snap_id=2, token_id=10, market_cap=1010)
    history = feature_history([t0, t1], t1)
    later_observation = TokenRiskAssessment(
        token_id=10,
        scanned_at=BASE,
        observed_at=BASE + timedelta(days=2),
        status="scored",
        risk_score=90,
        explanation="later observation",
    )
    as_of = BASE + timedelta(minutes=15)
    hidden = reconstruct_opportunity(settings, token, history, [], [later_observation], [], as_of)
    assert hidden.security_coverage == "UNAVAILABLE"

    earlier_observation = TokenRiskAssessment(
        token_id=10,
        scanned_at=BASE + timedelta(days=2),
        observed_at=BASE,
        status="scored",
        risk_score=90,
        explanation="earlier observation",
    )
    visible = reconstruct_opportunity(settings, token, history, [], [earlier_observation], [], as_of)
    assert visible.security_coverage != "UNAVAILABLE"


def test_research_purge_holdout_boundaries() -> None:
    t0 = BASE
    h = t0 + timedelta(hours=48)
    purge_start = h - timedelta(minutes=PURGE_MINUTES)
    assert assign_split(t0, t0, h) == SPLIT_RESEARCH
    assert assign_split(purge_start - timedelta(microseconds=1), t0, h) == SPLIT_RESEARCH
    assert assign_split(purge_start, t0, h) == SPLIT_PURGE
    assert assign_split(h - timedelta(microseconds=1), t0, h) == SPLIT_PURGE
    assert assign_split(h, t0, h) == SPLIT_HOLDOUT


def test_overlap_is_independent_per_group() -> None:
    from app.services.point_in_time_validation import CandidateRecord

    settings = get_settings()
    low = _snap(0, snap_id=1, liquidity=None)
    later_g1 = _snap(10, snap_id=2, liquidity=None, market_cap=1010)
    a = CandidateRecord(
        token_id=1,
        token_address="Tok",
        symbol="T",
        snapshot=low,
        signal_at=BASE,
        split=SPLIT_RESEARCH,
        in_g1=False,
        in_g2=False,
        in_g3=False,
    )
    b = CandidateRecord(
        token_id=1,
        token_address="Tok",
        symbol="T",
        snapshot=later_g1,
        signal_at=BASE + timedelta(minutes=10),
        split=SPLIT_RESEARCH,
        in_g1=True,
        in_g2=False,
        in_g3=False,
    )
    apply_overlap([a, b])
    assert a.overlap[G0][OVERLAP_1H] == 1
    assert b.overlap[G0][OVERLAP_1H] == 0
    assert b.overlap[G1][OVERLAP_1H] == 1
    assert a.overlap[G1][OVERLAP_1H] == 0
    _ = settings


def test_missing_outcomes_are_not_zero_returns() -> None:
    settings = get_settings()
    snap = _snap(0, snap_id=1)
    result = pair_outcome(
        snap, [], horizon="one_hour", max_gap_minutes=30, fee_bps=30, slippage_bps=50, settings=settings
    )
    assert result["coverage_status"] == MISSING_OUTCOME
    assert result["gross_return"] is None
    assert result["net_return"] is None


def test_quality_filtered_is_not_an_entry_group() -> None:
    assert QUALITY_FILTERED not in {G0, G1, G2, G3}


def test_pipeline_does_not_use_stored_opportunity_or_mutate_history(client: TestClient) -> None:
    session = get_session_factory()()
    session.add(
        HistoricalEvaluation(
            started_at=BASE,
            finished_at=BASE,
            status="completed",
            horizon="next_observation",
            outcomes_measured=14040,
            explanation="preserve",
        )
    )
    session.add(
        QualityHistoricalEvaluation(
            started_at=BASE,
            finished_at=BASE,
            status="completed",
            horizon="next_observation",
            outcomes_measured=14240,
            explanation="preserve",
        )
    )
    token = Token(token_address="PitTok11111111111111111111111111111111", chain="solana", symbol="PIT")
    session.add(token)
    session.flush()
    session.add(
        OpportunityAssessment(
            token_id=token.id,
            assessed_at=BASE,
            status=SCORED,
            ranking_label=HIGH_OPPORTUNITY,
            opportunity_score=99,
            positive_opportunity_score=99,
            security_adjustment=0,
            confidence=1,
            data_completeness=1,
            explanation="must not be used as historical entry",
        )
    )
    session.add(
        TokenRiskAssessment(
            token_id=token.id,
            scanned_at=BASE + timedelta(days=3),
            status="scored",
            explanation="future security must not apply",
        )
    )
    session.add_all(
        [
            MarketSnapshot(
                token_id=token.id,
                market_cap=1000,
                fdv=1000,
                price_usd=0.001,
                liquidity_usd=None,
                pair_address="PairP",
                recorded_at=BASE,
            ),
            MarketSnapshot(
                token_id=token.id,
                market_cap=1010,
                fdv=1010,
                price_usd=0.00101,
                liquidity_usd=None,
                pair_address="PairP",
                recorded_at=BASE + timedelta(minutes=15),
            ),
        ]
    )
    session.commit()
    snap_n = session.scalar(select(func.count()).select_from(MarketSnapshot))
    hist_n = session.scalar(select(func.count()).select_from(HistoricalEvaluation))
    q_n = session.scalar(select(func.count()).select_from(QualityHistoricalEvaluation))
    opp_n = session.scalar(select(func.count()).select_from(OpportunityAssessment))
    payload = run_point_in_time_evaluation(session, get_settings(), token_address=token.token_address)
    assert payload["candidate_count"] == 2
    assert payload["groups_before_overlap"]["G1"] >= 1
    assert payload["groups_before_overlap"]["G3"] == 0
    assert payload["g1_missing_liquidity"]["count"] >= 1
    first = [c for c in (payload,)][0]
    assert first["candidate_count"] == 2
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == snap_n
    assert session.scalar(select(func.count()).select_from(HistoricalEvaluation)) == hist_n
    assert session.scalar(select(func.count()).select_from(QualityHistoricalEvaluation)) == q_n
    assert session.scalar(select(func.count()).select_from(OpportunityAssessment)) == opp_n
    session.close()

    scanned = client.post(
        "/api/v1/historical/point-in-time-evaluate",
        json={"token_address": token.token_address},
    )
    assert scanned.status_code == 200
    body = scanned.json()
    assert "QUALITY_FILTERED" not in json_groups(body)
    assert body["g3_count"] == 0
    assert body["groups_before_overlap"]["G0"] == body["candidate_count"]
    status_payload = client.get("/api/v1/historical/point-in-time-status")
    assert status_payload.status_code == 200
    listed = client.get("/api/v1/historical/point-in-time-runs")
    assert listed.status_code == 200
    run_id = body["run_id"]
    fetched = client.get(f"/api/v1/historical/point-in-time-runs/{run_id}?group=G1&overlap=baseline&split=research")
    assert fetched.status_code == 200


def json_groups(body: dict) -> str:
    return str(body.get("groups_before_overlap"))
