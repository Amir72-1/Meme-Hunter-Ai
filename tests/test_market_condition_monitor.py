"""Phase 9C.3 null-aware market and liquidity monitoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import get_session_factory
from app.models import HistoricalEvaluation, MarketSnapshot, PointInTimeRun, Token
from app.services.market_condition_monitor import (
    COLLECTOR_FAILURE,
    COLLECTOR_NO_NEW_SNAPSHOTS,
    COLLECTOR_STOPPED,
    DUPLICATE_SNAPSHOT_TIMESTAMP,
    EXTREME_MARKET_CAP_CHANGE,
    INSUFFICIENT_DATA,
    INSUFFICIENT_LIQUIDITY_EVIDENCE,
    LIQUIDITY_DECREASE_40PCT,
    LIQUIDITY_INCREASE,
    LIQUIDITY_TIME_GAP_EXCEEDED,
    MARKET_DATA_PROVIDER,
    MARKET_STALE,
    MAX_SCAN_LIMIT,
    PAIR_ADDRESS_CHANGED,
    VALUATION_SOURCE_CHANGED,
    VERIFIED_ABSENT,
    VERIFIED_PRESENT,
    ZERO_LIQUIDITY,
    CollectorContext,
    analyze_token_market_conditions,
    classify_collector_health,
    finding_by_code,
    report_for_token,
    resolve_scan_limit,
    run_market_monitor_scan,
    snapshots_as_of,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from tests.test_security import _clean

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
ADDR = "So11111111111111111111111111111111111111112"
PAIR_A = "PairA1111111111111111111111111111111111111"
PAIR_B = "PairB1111111111111111111111111111111111111"


def _snap(**kwargs) -> MarketSnapshot:
    values = dict(
        token_id=1,
        recorded_at=T0,
        liquidity_usd=100.0,
        market_cap=1000.0,
        fdv=None,
        pair_address=PAIR_A,
        discovery_source="dexscreener",
        price_usd=1.0,
    )
    values.update(kwargs)
    return MarketSnapshot(**values)


def _analyze(snaps, when=None, collector=None):
    return analyze_token_market_conditions(
        ADDR,
        snaps,
        signal_time=when or T0,
        collector=collector
        or CollectorContext(
            latest_global_snapshot_at=when or T0,
            last_discovery_status="completed",
            last_discovery_finished_at=when or T0,
        ),
    )


def test_liquidity_decrease_of_40_percent_or_more() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=15), liquidity_usd=100.0)
    curr = _snap(recorded_at=T0, liquidity_usd=50.0)
    prev.id, curr.id = 1, 2
    report = _analyze([prev, curr])
    item = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert item.percentage_change == -0.5
    assert item.previous_value == 100.0
    assert item.current_value == 50.0
    assert item.previous_observation_id == 1
    assert item.current_observation_id == 2
    assert "rug" in item.limitations.lower()


def test_liquidity_increase() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=10), liquidity_usd=100.0)
    curr = _snap(recorded_at=T0, liquidity_usd=150.0)
    report = _analyze([prev, curr])
    drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    up = finding_by_code(report.findings, LIQUIDITY_INCREASE)
    assert drop is not None and drop.status == VERIFIED_ABSENT
    assert up is not None and up.status == VERIFIED_PRESENT
    assert up.percentage_change == 0.5


def test_missing_previous_liquidity() -> None:
    curr = _snap(recorded_at=T0, liquidity_usd=80.0)
    report = _analyze([curr])
    item = finding_by_code(report.findings, INSUFFICIENT_LIQUIDITY_EVIDENCE)
    assert item is not None
    assert item.status == INSUFFICIENT_DATA
    assert finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT) is None


def test_missing_current_liquidity() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=10), liquidity_usd=100.0)
    curr = _snap(recorded_at=T0, liquidity_usd=None)
    report = _analyze([prev, curr])
    item = finding_by_code(report.findings, INSUFFICIENT_LIQUIDITY_EVIDENCE)
    assert item is not None
    assert item.status == INSUFFICIENT_DATA
    assert item.current_value is None
    assert finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT) is None
    liq = report.availability["liquidity"]
    assert liq["numerator_missing"] == 1
    assert liq["denominator"] == 2


def test_zero_liquidity_versus_null_liquidity() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=5), liquidity_usd=100.0)
    zero = _snap(recorded_at=T0, liquidity_usd=0.0)
    null = _snap(recorded_at=T0, liquidity_usd=None)
    zero_report = _analyze([prev, zero])
    null_report = _analyze([prev, null])
    drop = finding_by_code(zero_report.findings, LIQUIDITY_DECREASE_40PCT)
    zero_flag = finding_by_code(zero_report.findings, ZERO_LIQUIDITY)
    assert drop is not None and drop.status == VERIFIED_PRESENT
    assert drop.current_value == 0.0
    assert drop.percentage_change == -1.0
    assert zero_flag is not None and zero_flag.status == VERIFIED_PRESENT
    null_drop = finding_by_code(null_report.findings, LIQUIDITY_DECREASE_40PCT)
    assert null_drop is None
    assert finding_by_code(null_report.findings, INSUFFICIENT_LIQUIDITY_EVIDENCE).status == INSUFFICIENT_DATA
    assert finding_by_code(null_report.findings, ZERO_LIQUIDITY) is None


def test_time_gap_exceeding_30_minutes() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=31), liquidity_usd=100.0)
    curr = _snap(recorded_at=T0, liquidity_usd=40.0)
    report = _analyze([prev, curr])
    gap = finding_by_code(report.findings, LIQUIDITY_TIME_GAP_EXCEEDED)
    assert gap is not None
    assert gap.status == INSUFFICIENT_DATA
    assert finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT) is None


def test_stale_market_observations() -> None:
    snap = _snap(recorded_at=T0 - timedelta(hours=2), liquidity_usd=100.0)
    report = _analyze([snap], when=T0)
    stale = finding_by_code(report.findings, MARKET_STALE)
    collector = finding_by_code(report.findings, COLLECTOR_FAILURE)
    assert stale is not None and stale.status == VERIFIED_PRESENT
    assert "30m" in stale.evidence["thresholds_crossed"]
    assert "1h" in stale.evidence["thresholds_crossed"]
    assert "6h" not in stale.evidence["thresholds_crossed"]
    assert collector is not None and collector.status == VERIFIED_ABSENT


def test_market_cap_fdv_source_changes() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=10), market_cap=1000.0, fdv=None)
    curr = _snap(recorded_at=T0, market_cap=None, fdv=5000.0, liquidity_usd=100.0)
    report = _analyze([prev, curr])
    changed = finding_by_code(report.findings, VALUATION_SOURCE_CHANGED)
    extreme = finding_by_code(report.findings, EXTREME_MARKET_CAP_CHANGE)
    assert changed is not None and changed.status == VERIFIED_PRESENT
    assert changed.previous_value == "market_cap"
    assert changed.current_value == "fdv"
    assert extreme is None


def test_extreme_same_source_market_cap_change() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=10), market_cap=1000.0, fdv=None, liquidity_usd=100.0)
    curr = _snap(recorded_at=T0, market_cap=20000.0, fdv=None, liquidity_usd=100.0)
    report = _analyze([prev, curr])
    extreme = finding_by_code(report.findings, EXTREME_MARKET_CAP_CHANGE)
    source = finding_by_code(report.findings, VALUATION_SOURCE_CHANGED)
    assert extreme is not None and extreme.status == VERIFIED_PRESENT
    assert source is not None and source.status == VERIFIED_ABSENT


def test_pair_address_changes() -> None:
    prev = _snap(recorded_at=T0 - timedelta(minutes=10), pair_address=PAIR_A)
    curr = _snap(recorded_at=T0, pair_address=PAIR_B)
    report = _analyze([prev, curr])
    item = finding_by_code(report.findings, PAIR_ADDRESS_CHANGED)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert item.previous_value == PAIR_A
    assert item.current_value == PAIR_B
    assert "malicious" in item.limitations.lower()


def test_duplicate_timestamps() -> None:
    first = _snap(recorded_at=T0, liquidity_usd=100.0)
    second = _snap(recorded_at=T0, liquidity_usd=50.0)
    first.id, second.id = 3, 4
    report = _analyze([first, second])
    dup = finding_by_code(report.findings, DUPLICATE_SNAPSHOT_TIMESTAMP)
    drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    assert dup is not None and dup.status == VERIFIED_PRESENT
    assert drop is not None and drop.status == VERIFIED_PRESENT


def test_point_in_time_excludes_later_observations() -> None:
    early = _snap(recorded_at=T0, liquidity_usd=100.0)
    later = _snap(recorded_at=T0 + timedelta(hours=1), liquidity_usd=10.0)
    early.id, later.id = 8, 9
    kept = snapshots_as_of([early, later], T0)
    assert [item.id for item in kept] == [8]
    report = _analyze([early, later], when=T0)
    drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    assert drop is None
    assert report.snapshot_count == 1


def test_snapshot_immutability_and_historical_eval_unchanged() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=ADDR, name="Mon", symbol="MON", chain="solana", pair_address=PAIR_A)
    eval_row = HistoricalEvaluation(horizon="next_observation", explanation="keep-9c3")
    pit_row = PointInTimeRun(status="completed", candidates_count=2, summary_json='{"keep":true}')
    session.add_all([token, eval_row, pit_row])
    session.commit()
    snap = MarketSnapshot(
        token_id=token.id,
        recorded_at=T0,
        liquidity_usd=250.0,
        market_cap=2000.0,
        pair_address=PAIR_A,
        price_usd=1.2,
    )
    session.add(snap)
    session.commit()
    snap_id, liq, mcap = snap.id, snap.liquidity_usd, snap.market_cap
    eval_id, text = eval_row.id, eval_row.explanation
    pit_id, pit_json = pit_row.id, pit_row.summary_json
    run_market_monitor_scan(session, token_address=ADDR, signal_time=T0)
    kept = session.get(MarketSnapshot, snap_id)
    assert kept is not None
    assert kept.liquidity_usd == liq
    assert kept.market_cap == mcap
    assert session.get(HistoricalEvaluation, eval_id).explanation == text
    assert session.get(PointInTimeRun, pit_id).summary_json == pit_json
    session.close()


def test_opportunity_scores_and_thresholds_unchanged() -> None:
    settings = get_settings()
    assert settings.opportunity_min_completeness == 0.40
    assert settings.opportunity_min_confidence == 0.50
    assert settings.opportunity_high_threshold == 70.0
    assert settings.security_min_completeness == 0.50
    assert settings.security_scan_limit == 25
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=80,
        opportunity_score=80,
        data_completeness=0.80,
        confidence=0.70,
    )
    assert status == "SCORED"
    assert label == HIGH_OPPORTUNITY


def test_null_previous_skipped_for_non_null_compare() -> None:
    missing = _snap(recorded_at=T0 - timedelta(minutes=8), liquidity_usd=None)
    prev = _snap(recorded_at=T0 - timedelta(minutes=6), liquidity_usd=200.0)
    curr = _snap(recorded_at=T0, liquidity_usd=100.0)
    prev.id, curr.id = 11, 12
    report = _analyze([missing, prev, curr])
    drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    assert drop is not None
    assert drop.status == VERIFIED_PRESENT
    assert drop.previous_observation_id == 11
    assert drop.percentage_change == -0.5


def test_report_for_token_persisted_and_scan_bounded() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=ADDR, name="Mon", symbol="MON", chain="solana")
    session.add(token)
    session.commit()
    session.add_all(
        [
            MarketSnapshot(
                token_id=token.id,
                recorded_at=T0 - timedelta(minutes=12),
                liquidity_usd=100.0,
                market_cap=1000.0,
                pair_address=PAIR_A,
            ),
            MarketSnapshot(
                token_id=token.id,
                recorded_at=T0,
                liquidity_usd=40.0,
                market_cap=1000.0,
                pair_address=PAIR_A,
            ),
        ]
    )
    session.commit()
    report = report_for_token(session, token, T0)
    drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
    assert drop is not None and drop.status == VERIFIED_PRESENT
    payload = run_market_monitor_scan(session, token_address=ADDR, limit=25, signal_time=T0)
    assert payload["tokens_scanned"] == 1
    assert payload["liquidity_decreases"] == 1
    session.close()


def _disc(status: str, saved: int, ago_min: int, sid: int) -> dict:
    at = T0 - timedelta(minutes=ago_min)
    return {"id": sid, "status": status, "started_at": at, "finished_at": at, "snapshots_saved": saved}


def _collector(global_ago_min: int, scans: list[dict]) -> CollectorContext:
    global_at = T0 - timedelta(minutes=global_ago_min)
    last = scans[0] if scans else None
    return CollectorContext(
        latest_global_snapshot_at=global_at,
        last_discovery_status=last["status"] if last else None,
        last_discovery_finished_at=last["finished_at"] if last else global_at,
        recent_scans=scans,
    )


def test_one_failed_scan_with_recent_successful_scans() -> None:
    collector = _collector(
        5,
        [_disc("failed", 0, 4, 9), _disc("completed", 200, 16, 8), _disc("completed", 180, 31, 7)],
    )
    classified = classify_collector_health(collector, T0)
    assert classified["failure_status"] == VERIFIED_ABSENT
    assert classified["health"] == "running"
    snap = _snap(recorded_at=T0 - timedelta(minutes=5), liquidity_usd=100.0)
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_ABSENT
    assert finding_by_code(report.findings, COLLECTOR_STOPPED).status == VERIFIED_ABSENT


def test_repeated_failed_scans() -> None:
    collector = _collector(
        240,
        [_disc("failed", 0, 5, 12), _disc("failed", 0, 20, 11), _disc("failed", 0, 35, 10)],
    )
    classified = classify_collector_health(collector, T0)
    assert classified["consecutive_failed"] >= 3
    assert classified["failure_status"] == VERIFIED_PRESENT
    assert classified["health"] == "failing"
    snap = _snap(recorded_at=T0 - timedelta(hours=4), liquidity_usd=100.0)
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_PRESENT
    assert finding_by_code(report.findings, MARKET_STALE).status == VERIFIED_PRESENT


def test_successful_scan_with_zero_new_snapshots() -> None:
    collector = _collector(240, [_disc("completed", 0, 5, 4)])
    classified = classify_collector_health(collector, T0)
    assert classified["failure_status"] == VERIFIED_ABSENT
    assert classified["no_snapshots_status"] == VERIFIED_ABSENT
    snap = _snap(recorded_at=T0 - timedelta(hours=4), liquidity_usd=50.0)
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_ABSENT
    assert finding_by_code(report.findings, COLLECTOR_NO_NEW_SNAPSHOTS).status == VERIFIED_ABSENT


def test_repeated_successful_scans_with_no_new_snapshots() -> None:
    collector = _collector(
        240,
        [_disc("completed", 0, 5, 6), _disc("completed", 0, 20, 5), _disc("completed", 0, 35, 4)],
    )
    classified = classify_collector_health(collector, T0)
    assert classified["no_snapshots_status"] == VERIFIED_PRESENT
    assert classified["failure_status"] == VERIFIED_ABSENT
    assert classified["health"] == "running_without_snapshots"
    snap = _snap(recorded_at=T0 - timedelta(hours=4), liquidity_usd=50.0)
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, COLLECTOR_NO_NEW_SNAPSHOTS).status == VERIFIED_PRESENT
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_ABSENT


def test_backend_stopped_with_stale_global_snapshots() -> None:
    collector = _collector(240, [_disc("completed", 200, 240, 3)])
    classified = classify_collector_health(collector, T0)
    assert classified["stopped_status"] == VERIFIED_PRESENT
    assert classified["failure_status"] == VERIFIED_ABSENT
    snap = _snap(recorded_at=T0 - timedelta(hours=4), liquidity_usd=80.0)
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, COLLECTOR_STOPPED).status == VERIFIED_PRESENT
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_ABSENT
    assert finding_by_code(report.findings, MARKET_STALE).status == VERIFIED_PRESENT


def test_stale_token_while_collector_healthy() -> None:
    collector = _collector(5, [_disc("completed", 214, 5, 20)])
    snap = _snap(recorded_at=T0 - timedelta(hours=2), liquidity_usd=90.0, discovery_source="profile")
    report = _analyze([snap], collector=collector)
    assert finding_by_code(report.findings, MARKET_STALE).status == VERIFIED_PRESENT
    assert finding_by_code(report.findings, COLLECTOR_FAILURE).status == VERIFIED_ABSENT
    assert finding_by_code(report.findings, COLLECTOR_STOPPED).status == VERIFIED_ABSENT
    stale = finding_by_code(report.findings, MARKET_STALE)
    assert stale.data_source == MARKET_DATA_PROVIDER
    assert stale.market_data_provider == MARKET_DATA_PROVIDER
    assert stale.discovery_source == "profile"


def test_discovery_source_is_not_market_data_provider() -> None:
    for channel in ("profile", "latest_boosts", "top_boosts"):
        prev = _snap(recorded_at=T0 - timedelta(minutes=10), liquidity_usd=100.0, discovery_source=channel)
        curr = _snap(recorded_at=T0, liquidity_usd=50.0, discovery_source=channel)
        report = _analyze([prev, curr])
        drop = finding_by_code(report.findings, LIQUIDITY_DECREASE_40PCT)
        assert drop is not None
        assert drop.data_source == "dexscreener"
        assert drop.market_data_provider == "dexscreener"
        assert drop.discovery_source == channel
        assert drop.discovery_source != drop.market_data_provider or channel == "dexscreener"


def test_requested_limit_versus_maximum_limit() -> None:
    assert resolve_scan_limit(5, token_address=None) == (5, 5)
    assert resolve_scan_limit(25, token_address=None) == (25, 25)
    assert resolve_scan_limit(50, token_address=None) == (50, 50)
    assert resolve_scan_limit(50, token_address=None)[1] == MAX_SCAN_LIMIT
    assert resolve_scan_limit(25, token_address=ADDR) == (25, None)


def test_scan_limit_metadata_for_5_25_and_50() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=ADDR, name="Lim", symbol="LIM", chain="solana")
    session.add(token)
    session.commit()
    session.add(MarketSnapshot(token_id=token.id, recorded_at=T0, liquidity_usd=100.0, market_cap=1.0))
    session.commit()
    for requested in (5, 25, 50):
        payload = run_market_monitor_scan(session, limit=requested, signal_time=T0)
        assert payload["requested_limit"] == requested
        assert payload["applied_limit"] == requested
        assert payload["max_scan_limit"] == 50
        assert payload["tokens_scanned"] == 1
        assert payload["thresholds"]["requested_limit"] == requested
        assert payload["thresholds"]["applied_limit"] == requested
        assert payload["thresholds"]["max_scan_limit"] == 50
        assert payload["thresholds"].get("scan_limit") != 50 or requested == 50
    session.close()
