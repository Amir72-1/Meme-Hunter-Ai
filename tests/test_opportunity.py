"""Opportunity scoring and scan tests. No live DexScreener or Solana RPC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    Narrative,
    OpportunityAssessment,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
    utc_now,
)
from app.services.opportunity_scan import scan_opportunities
from app.services.opportunity_scoring import (
    DISCOVERY,
    HIGH_OPPORTUNITY,
    INSUFFICIENT_DATA,
    LIQUIDITY,
    LOW_CONFIDENCE,
    LOW_OPPORTUNITY,
    MARKET,
    NARRATIVE,
    SCORED,
    SECURITY,
    WATCHLIST,
    assess_opportunity,
    analyze_market_history,
    classify_opportunity,
    normalize_positive_score,
    score_market_momentum,
    snapshots_as_of,
)
from app.services.opportunity_scoring import ComponentResult

TOKEN_A = "OppA111111111111111111111111111111111111111"
TOKEN_B = "OppB222222222222222222222222222222222222222"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _clean(session) -> None:
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(TokenNarrativeMatch))
    session.execute(delete(TokenRiskAssessment))
    session.execute(delete(LiquidityAnalysisRecord))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(Narrative))
    session.execute(delete(Token))
    session.execute(delete(IntelligenceScan))
    session.commit()


def _token(**kwargs) -> Token:
    payload = {
        "token_address": TOKEN_A,
        "name": "Alpha",
        "symbol": "ALP",
        "chain": "solana",
        "pair_created_at": NOW - timedelta(hours=12),
        "last_discovered_at": NOW - timedelta(hours=10),
        "created_at": NOW - timedelta(hours=10),
    }
    payload.update(kwargs)
    return Token(**payload)


def _snap(hours_ago: float, **kwargs) -> MarketSnapshot:
    payload = {
        "token_id": 1,
        "price_usd": 0.02,
        "liquidity_usd": 25000,
        "volume_24h": 8000,
        "buys_24h": 40,
        "sells_24h": 20,
        "recorded_at": NOW - timedelta(hours=hours_ago),
    }
    payload.update(kwargs)
    return MarketSnapshot(**payload)


def test_weighted_normalization_skips_missing_components() -> None:
    measured = [
        ComponentResult(MARKET, 0.25, 80, 0.7, "MEASURED", "m"),
        ComponentResult(LIQUIDITY, 0.10, 40, 0.6, "MEASURED", "l"),
    ]
    # (80*0.25 + 40*0.10) / 0.35 = 68.571...
    assert normalize_positive_score(measured) == 68.6
    missing_as_none = measured + [ComponentResult(NARRATIVE, 0.25, None, 0.0, "UNAVAILABLE", "n")]
    assert normalize_positive_score(missing_as_none) == 68.6


def test_missing_component_is_not_fabricated_zero() -> None:
    settings = get_settings()
    result = assess_opportunity(settings, _token(), [_snap(1)], [], None, None, now=NOW)
    assert NARRATIVE in result.missing_components
    assert SECURITY in result.missing_components
    narrative = next(item for item in result.components if item.name == NARRATIVE)
    assert narrative.score is None
    assert "zero" in narrative.explanation.lower() or "not treated as zero" in narrative.explanation.lower()


def test_single_snapshot_does_not_compute_local_growth() -> None:
    settings = get_settings()
    snaps = [_snap(1, volume_24h=9000, price_change_24h=12, liquidity_usd=25000)]
    history = analyze_market_history(snaps, NOW, settings.opportunity_stale_snapshot_hours)
    assert history["snapshot_count"] == 1
    assert history["price_change"]["status"] == "UNAVAILABLE"
    assert history["liquidity_change"]["status"] == "UNAVAILABLE"
    assert history["volume_change"]["status"] == "UNAVAILABLE"
    assert history["txn_growth"]["status"] == "UNAVAILABLE"
    assert history["acceleration"]["status"] == "UNAVAILABLE"
    component = score_market_momentum(snaps, settings, NOW, settings.opportunity_weight_momentum)
    assert component.score is None
    assert component.status == "UNAVAILABLE"
    assert any("single snapshot" in warning.lower() for warning in component.warnings)


def test_market_momentum_uses_historical_snapshots() -> None:
    settings = get_settings()
    snaps = [
        _snap(5, volume_24h=2000, price_usd=0.01, liquidity_usd=18000, buys_24h=10, sells_24h=10),
        _snap(3, volume_24h=4000, price_usd=0.015, liquidity_usd=20000, buys_24h=20, sells_24h=12),
        _snap(1, volume_24h=8000, price_usd=0.02, liquidity_usd=24000, buys_24h=40, sells_24h=15),
    ]
    history = analyze_market_history(snaps, NOW, settings.opportunity_stale_snapshot_hours)
    assert history["snapshot_count"] == 3
    assert history["price_change"]["status"] == "MEASURED"
    assert history["liquidity_change"]["status"] == "MEASURED"
    assert history["volume_change"]["status"] == "MEASURED"
    assert history["txn_growth"]["status"] == "MEASURED"
    assert history["acceleration"]["status"] == "MEASURED"
    component = score_market_momentum(snaps, settings, NOW, settings.opportunity_weight_momentum)
    assert component.status == "MEASURED"
    assert component.score is not None
    assert any("growth" in signal for signal in component.signals)


def test_future_snapshots_are_ignored() -> None:
    past = _snap(2, volume_24h=4000, price_usd=0.01)
    present = _snap(1, volume_24h=8000, price_usd=0.02)
    future = _snap(-3, volume_24h=99999, price_usd=9.99)
    kept = snapshots_as_of([past, present, future], NOW)
    assert future not in kept
    history = analyze_market_history(kept, NOW, 6.0)
    assert history["snapshot_count"] == 2
    result = assess_opportunity(get_settings(), _token(), [past, present, future], [], None, None, now=NOW)
    assert result.snapshot_count == 2


def test_zero_previous_volume_does_not_divide() -> None:
    settings = get_settings()
    component = score_market_momentum(
        [
            _snap(3, volume_24h=0, price_usd=0.01),
            _snap(1, volume_24h=8000, price_usd=0.02),
        ],
        settings,
        NOW,
        settings.opportunity_weight_momentum,
    )
    assert all("volume_24h growth" not in signal for signal in component.signals)


def test_narrative_integration_and_name_only_ignored() -> None:
    settings = get_settings()
    narrative = Narrative(id=1, name="AI agents", trend_score=80, mention_growth=0.5, independent_sources=3)
    weak = TokenNarrativeMatch(
        token_id=1,
        narrative_id=1,
        explanation="name only",
        confidence=0.28,
        method="name_keyword_hypothesis",
    )
    strong = TokenNarrativeMatch(
        token_id=1,
        narrative_id=1,
        explanation="symbol and source",
        confidence=0.86,
        method="symbol_and_source",
    )
    missing = assess_opportunity(settings, _token(), [_snap(1)], [(weak, narrative)], None, None, now=NOW)
    assert NARRATIVE in missing.missing_components
    linked = assess_opportunity(settings, _token(), [_snap(1)], [(strong, narrative)], None, None, now=NOW)
    component = next(item for item in linked.components if item.name == NARRATIVE)
    assert component.score == pytest.approx(80 * 0.86, abs=0.2)
    assert component.status == "MEASURED"


def test_liquidity_quality_and_unknown_lock() -> None:
    settings = get_settings()
    record = LiquidityAnalysisRecord(
        token_id=1,
        lock_status="UNKNOWN",
        lock_evidence="not independently verified",
        liquidity_usd=25000,
        scanned_at=NOW,
    )
    result = assess_opportunity(settings, _token(), [_snap(1, liquidity_usd=25000)], [], None, record, now=NOW)
    liq = next(item for item in result.components if item.name == LIQUIDITY)
    assert liq.score is not None
    assert any("UNKNOWN" in warning for warning in liq.warnings)


def test_confirmed_security_penalty() -> None:
    settings = get_settings()
    risk = TokenRiskAssessment(
        token_id=1,
        scanned_at=NOW,
        status="scored",
        observed_risk=80,
        observed_risk_band="critical",
        risk_score=80,
        risk_band="critical",
        confidence=0.9,
        completeness=1.0,
        explanation="scored",
        category_scores_json="[]",
    )
    result = assess_opportunity(
        settings,
        _token(),
        [_snap(3, volume_24h=4000), _snap(1, volume_24h=9000)],
        [],
        risk,
        None,
        now=NOW,
    )
    assert result.security_adjustment == pytest.approx(-16.0)
    assert result.positive_opportunity_score is not None
    assert result.opportunity_score == pytest.approx(
        max(0.0, result.positive_opportunity_score - 16.0), abs=0.11
    )
    security = next(item for item in result.components if item.name == SECURITY)
    assert security.status == "CONFIRMED"


def test_unknown_and_low_confidence_security_are_not_confirmed_penalties() -> None:
    settings = get_settings()
    unknown = assess_opportunity(settings, _token(), [_snap(1)], [], None, None, now=NOW)
    assert unknown.security_adjustment == 0
    assert SECURITY in unknown.missing_components
    assert any("not evidence of safety" in item.explanation.lower() for item in unknown.components if item.name == SECURITY)

    weak_risk = TokenRiskAssessment(
        token_id=1,
        scanned_at=NOW,
        status="scored",
        observed_risk=None,
        risk_score=75,
        risk_band="high",
        confidence=0.364,
        completeness=0.4,
        explanation="legacy scored",
        category_scores_json=(
            '[{"name":"token_authorities","status":"UNKNOWN","weight":0.2,"score":null,"reason":"unavailable"}]'
        ),
    )
    weak = assess_opportunity(settings, _token(), [_snap(1)], [], weak_risk, None, now=NOW)
    assert weak.security_adjustment == 0
    security = next(item for item in weak.components if item.name == SECURITY)
    assert security.status == "UNCONFIRMED"
    assert any("UNKNOWN" in warning for warning in security.warnings)


def test_high_score_with_low_confidence_is_not_high_opportunity() -> None:
    settings = get_settings()
    narrative = Narrative(id=1, name="AI agents", trend_score=90)
    match = TokenNarrativeMatch(
        token_id=1,
        narrative_id=1,
        explanation="overlap",
        confidence=0.55,
        method="symbol_overlap",
    )
    result = assess_opportunity(
        settings,
        _token(),
        [_snap(1, price_change_24h=40, liquidity_usd=30000, volume_24h=20000, buys_24h=80, sells_24h=10)],
        [(match, narrative)],
        None,
        LiquidityAnalysisRecord(token_id=1, lock_status="UNKNOWN", scanned_at=NOW, liquidity_usd=30000),
        now=NOW,
    )
    assert result.data_completeness >= settings.opportunity_min_completeness
    assert result.confidence < settings.opportunity_min_confidence
    assert result.status == LOW_CONFIDENCE
    assert result.ranking_label != "HIGH_OPPORTUNITY"
    if result.opportunity_score and result.opportunity_score >= settings.opportunity_watch_threshold:
        assert result.ranking_label == WATCHLIST


def test_new_thin_liquidity_is_not_automatic_early_opportunity() -> None:
    settings = get_settings()
    token = _token(pair_created_at=NOW - timedelta(minutes=20))
    result = assess_opportunity(settings, token, [_snap(0.1, liquidity_usd=800)], [], None, None, now=NOW)
    discovery = next(item for item in result.components if item.name == DISCOVERY)
    assert discovery.score is not None
    assert discovery.score <= 35
    assert any("thin" in warning.lower() for warning in discovery.warnings)


def test_empty_discovery_scan() -> None:
    session = get_session_factory()()
    _clean(session)
    summary = scan_opportunities(session, get_settings(), force=True)
    assert summary.tokens_considered == 0
    assert summary.tokens_scanned == 0
    assert summary.status == "completed"
    session.close()


def test_scan_persists_history_and_survives_one_failure(monkeypatch) -> None:
    session = get_session_factory()()
    _clean(session)
    token_a = _token()
    token_b = _token(token_address=TOKEN_B, symbol="BET", name="Beta")
    session.add_all([token_a, token_b])
    session.commit()
    session.add_all(
        [
            _snap(2, token_id=token_a.id, volume_24h=4000),
            _snap(1, token_id=token_a.id, volume_24h=9000),
            _snap(1, token_id=token_b.id, volume_24h=5000),
        ]
    )
    session.commit()

    from app.services import opportunity_scan as module

    real = module.assess_stored_token

    def flaky(session_arg, settings, token):
        if token.token_address == TOKEN_B:
            raise RuntimeError("synthetic assessment failure")
        return real(session_arg, settings, token)

    monkeypatch.setattr(module, "assess_stored_token", flaky)
    summary = module.scan_opportunities(session, get_settings(), force=True)
    assert summary.tokens_scanned == 1
    assert summary.errors[0]["token_address"] == TOKEN_B
    rows = list(session.scalars(select(OpportunityAssessment)).all())
    assert len(rows) == 1
    first_id = rows[0].id
    monkeypatch.setattr(module, "assess_stored_token", real)
    again = module.scan_opportunities(session, get_settings(), force=True)
    assert again.tokens_scanned == 2
    history = list(session.scalars(select(OpportunityAssessment).order_by(OpportunityAssessment.id)).all())
    assert len(history) >= 3
    assert any(row.id == first_id for row in history)
    session.close()


def test_scan_counters_distinguish_low_confidence_from_insufficient() -> None:
    session = get_session_factory()()
    _clean(session)
    token = _token()
    session.add(token)
    session.commit()
    session.add_all(
        [
            _snap(2, token_id=token.id, volume_24h=4000, liquidity_usd=22000),
            _snap(1, token_id=token.id, volume_24h=9000, liquidity_usd=25000),
        ]
    )
    session.commit()
    summary = scan_opportunities(session, get_settings(), force=True)
    assert summary.tokens_scanned == 1
    assert summary.scored + summary.low_confidence + summary.insufficient_data == 1
    assert summary.reliable_classifications == summary.scored
    stored = session.scalar(select(OpportunityAssessment))
    assert stored is not None
    if stored.status == "LOW_CONFIDENCE":
        assert summary.low_confidence == 1
        assert summary.insufficient_data == 0
    session.close()


def test_ranking_order_prefers_higher_opportunity_score() -> None:
    settings = get_settings()
    weak = assess_opportunity(
        settings,
        _token(pair_created_at=NOW - timedelta(days=20)),
        [_snap(1, liquidity_usd=3000, volume_24h=500, buys_24h=2, sells_24h=20, price_change_24h=-12)],
        [],
        None,
        None,
        now=NOW,
    )
    strong_match = TokenNarrativeMatch(
        token_id=1, narrative_id=1, explanation="src", confidence=0.86, method="symbol_and_source"
    )
    narrative = Narrative(id=1, name="AI agents", trend_score=88, mention_growth=0.4, independent_sources=4)
    strong = assess_opportunity(
        settings,
        _token(),
        [
            _snap(4, volume_24h=3000, price_usd=0.01, liquidity_usd=20000),
            _snap(2, volume_24h=6000, price_usd=0.015, liquidity_usd=24000),
            _snap(1, volume_24h=12000, price_usd=0.025, liquidity_usd=28000, buys_24h=90, sells_24h=20),
        ],
        [(strong_match, narrative)],
        None,
        None,
        now=NOW,
    )
    assert strong.opportunity_score is not None
    assert weak.opportunity_score is not None
    assert strong.opportunity_score > weak.opportunity_score
    if strong.status == SCORED and strong.opportunity_score >= settings.opportunity_high_threshold:
        assert strong.ranking_label == "HIGH_OPPORTUNITY"
    assert weak.ranking_label in {WATCHLIST, LOW_OPPORTUNITY, INSUFFICIENT_DATA}


def test_low_completeness_keeps_observed_score_but_not_scored() -> None:
    settings = get_settings()
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=68.6,
        opportunity_score=68.6,
        data_completeness=0.35,
        confidence=0.41,
    )
    assert status == INSUFFICIENT_DATA
    assert label == INSUFFICIENT_DATA
    assert label != HIGH_OPPORTUNITY


def test_low_confidence_with_enough_completeness() -> None:
    settings = get_settings()
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=68.6,
        opportunity_score=68.6,
        data_completeness=0.40,
        confidence=0.41,
    )
    assert status == LOW_CONFIDENCE
    assert label != HIGH_OPPORTUNITY
    assert label == WATCHLIST


def test_scored_when_both_floors_met() -> None:
    settings = get_settings()
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=68.6,
        opportunity_score=68.6,
        data_completeness=0.40,
        confidence=0.50,
    )
    assert status == SCORED
    assert label == WATCHLIST


def test_no_positive_score_is_insufficient() -> None:
    settings = get_settings()
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=None,
        opportunity_score=None,
        data_completeness=0.80,
        confidence=0.90,
    )
    assert status == INSUFFICIENT_DATA
    assert label == INSUFFICIENT_DATA


def test_high_opportunity_requires_scored_score_and_confidence() -> None:
    settings = get_settings()
    high_status, high_label = classify_opportunity(
        settings,
        positive_opportunity_score=80,
        opportunity_score=80,
        data_completeness=0.80,
        confidence=0.70,
    )
    assert high_status == SCORED
    assert high_label == HIGH_OPPORTUNITY

    weak_conf_status, weak_conf_label = classify_opportunity(
        settings,
        positive_opportunity_score=80,
        opportunity_score=80,
        data_completeness=0.80,
        confidence=0.41,
    )
    assert weak_conf_status == LOW_CONFIDENCE
    assert weak_conf_label != HIGH_OPPORTUNITY
