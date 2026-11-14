"""Read-only data-readiness: missingness, coverage, scheduler health, immutability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import DiscoveryScan, HistoricalEvaluation, MarketSnapshot, Token
from app.services.data_readiness import (
    assemble_readiness,
    consecutive_failures,
    g3_exclusion_breakdown,
    missing_pct,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, SCORED

NOW = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
BASE = NOW - timedelta(days=2)


class _Sched:
    def __init__(self, enabled: bool = True, running: bool = True, busy: bool = False) -> None:
        self.enabled = enabled
        self.running = running
        self._busy = busy

    def is_busy(self) -> bool:
        return self._busy


def _token(tid: int, addr: str) -> Token:
    return Token(id=tid, token_address=addr, chain="solana", symbol="T")


def _snap(
    token_id: int,
    minutes: float,
    *,
    snap_id: int,
    liquidity: float | None = 5000,
    market_cap: float | None = 1000,
    pair: str = "PairA",
) -> MarketSnapshot:
    return MarketSnapshot(
        id=snap_id,
        token_id=token_id,
        market_cap=market_cap,
        fdv=market_cap,
        price_usd=0.001,
        liquidity_usd=liquidity,
        pair_address=pair,
        recorded_at=BASE + timedelta(minutes=minutes),
    )


def test_missing_pct_denominator() -> None:
    assert missing_pct(5, 10) == 0.5
    assert missing_pct(0, 0) is None
    assert missing_pct(0, 4) == 0.0


def test_consecutive_failures() -> None:
    assert consecutive_failures(["failed", "failed", "completed"]) == 2
    assert consecutive_failures(["completed", "failed"]) == 0
    assert consecutive_failures(["running", "failed", "completed"]) == 1
    assert consecutive_failures(["failed", "failed"]) == 2
    assert consecutive_failures(["interrupted", "failed", "completed"]) == 2


def test_g3_exclusion_reasons_do_not_relax_thresholds() -> None:
    settings = get_settings()
    rows = [
        {
            "in_g2": True,
            "in_g3": False,
            "opportunity_status": "LOW_CONFIDENCE",
            "ranking_label": "WATCHLIST",
            "confidence": 0.40,
            "completeness": 0.55,
            "opportunity_score": 95.0,
        }
    ]
    payload = g3_exclusion_breakdown(
        rows,
        min_confidence=settings.opportunity_min_confidence,
        min_completeness=settings.opportunity_min_completeness,
        high_threshold=settings.opportunity_high_threshold,
    )
    assert payload["g3_count"] == 0
    assert payload["exclusion_reason_counts"]["not_scored"] == 1
    assert payload["exclusion_reason_counts"]["confidence_below_floor"] == 1
    assert payload["exclusion_reason_counts"]["not_high_opportunity"] == 1
    assert "score_below_high_threshold" not in payload["exclusion_reason_counts"]
    assert settings.opportunity_min_confidence == 0.50
    assert SCORED != "LOW_CONFIDENCE"
    assert HIGH_OPPORTUNITY != "WATCHLIST"


def test_null_liquidity_and_coverage_math() -> None:
    settings = get_settings()
    tokens = [_token(1, "TokA"), _token(2, "TokB")]
    snaps = [
        _snap(1, 0, snap_id=1, liquidity=None),
        _snap(1, 15, snap_id=2, liquidity=None, pair="PairB"),
        _snap(1, 30, snap_id=3, liquidity=12000),
        _snap(2, 0, snap_id=4, liquidity=8000),
    ]
    report = assemble_readiness(
        tokens=tokens,
        snapshots=snaps,
        discovery_scans=[],
        risk_rows=[],
        security_scans=[],
        narrative_matches=[],
        opportunities=[],
        historical_evals=[],
        quality_evals=[],
        pit_run=None,
        pit_candidates=[],
        settings=settings,
        scheduler=_Sched(enabled=True, running=False),
        now=NOW,
    )
    assert report["liquidity"]["missing"] == 2
    assert report["liquidity"]["total_observations"] == 4
    assert report["liquidity"]["missing_pct"] == 0.5
    assert "zero" in report["liquidity"]["note"].lower()
    assert report["observation_counts"]["at_least_2"] == 1
    assert report["pair_changes"]["tokens_with_multiple_pairs"] == 1
    assert report["security"]["tokens_with_risk_assessment"] == 0
    assert report["narratives"]["tokens_with_match"] == 0
    assert report["collection_health"]["collection_healthy"] is False
    assert "scheduler_not_running" in report["collection_health"]["unhealthy_reasons"]


def test_stale_snapshot_detection() -> None:
    settings = get_settings()
    token = _token(1, "StaleTok")
    old = MarketSnapshot(
        id=1,
        token_id=1,
        market_cap=1000,
        fdv=1000,
        price_usd=0.001,
        liquidity_usd=1000,
        pair_address="P",
        recorded_at=NOW - timedelta(hours=10),
    )
    report = assemble_readiness(
        tokens=[token],
        snapshots=[old],
        discovery_scans=[],
        risk_rows=[],
        security_scans=[],
        narrative_matches=[],
        opportunities=[],
        historical_evals=[],
        quality_evals=[],
        pit_run=None,
        pit_candidates=[],
        settings=settings,
        scheduler=_Sched(),
        now=NOW,
    )
    assert report["stale_snapshots"]["stale_token_count"] == 1
    assert report["history_span"]["at_least_1_days"] == 0


def test_scheduler_health_requires_recent_snapshots() -> None:
    settings = get_settings()
    token = _token(1, "Tok")
    snap = _snap(1, 0, snap_id=1)
    snap.recorded_at = NOW - timedelta(hours=2)
    scan = DiscoveryScan(id=9, started_at=NOW - timedelta(hours=3), status="completed")
    report = assemble_readiness(
        tokens=[token],
        snapshots=[snap],
        discovery_scans=[scan],
        risk_rows=[],
        security_scans=[],
        narrative_matches=[],
        opportunities=[],
        historical_evals=[],
        quality_evals=[],
        pit_run=None,
        pit_candidates=[],
        settings=settings,
        scheduler=_Sched(enabled=True, running=True),
        now=NOW,
    )
    assert report["collection_health"]["scheduler_enabled"] is True
    assert report["collection_health"]["recent_snapshots"] is False
    assert report["collection_health"]["collection_healthy"] is False
    assert "snapshots_not_recent" in report["collection_health"]["unhealthy_reasons"]


def test_readiness_api_does_not_mutate_snapshots(client: TestClient) -> None:
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
    token = Token(token_address="ReadyTok11111111111111111111111111111", chain="solana", symbol="RDY")
    session.add(token)
    session.flush()
    session.add(
        MarketSnapshot(
            token_id=token.id,
            market_cap=1000,
            fdv=1000,
            price_usd=0.001,
            liquidity_usd=None,
            pair_address="PairR",
            recorded_at=BASE,
        )
    )
    session.commit()
    snap_n = session.scalar(select(func.count()).select_from(MarketSnapshot))
    eval_n = session.scalar(select(func.count()).select_from(HistoricalEvaluation))
    session.close()

    payload = client.get("/api/v1/readiness")
    assert payload.status_code == 200
    body = payload.json()
    assert body["liquidity"]["missing"] >= 1
    assert body["g3"]["g3_count"] == 0
    health = client.get("/api/v1/readiness/health")
    assert health.status_code == 200
    assert "collection_healthy" in health.json()

    session = get_session_factory()()
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == snap_n
    assert session.scalar(select(func.count()).select_from(HistoricalEvaluation)) == eval_n
    session.close()
