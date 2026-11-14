"""Read-only collection and data-readiness reports. Does not rewrite history."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    DiscoveryScan,
    HistoricalEvaluation,
    MarketSnapshot,
    OpportunityAssessment,
    PointInTimeCandidate,
    PointInTimeRun,
    QualityHistoricalEvaluation,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
    TokenSecurityScan,
)
from app.services.discovery_recovery import (
    FAILURE_STATUSES,
    collection_continuity,
    consecutive_failures,
)
from app.services.discovery_scheduler import DiscoveryScheduler, get_scheduler
from app.services.historical_evaluation import valuation_usd
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, SCORED
from app.timeutil import as_utc, utc_now

OBSERVATION_THRESHOLDS = (2, 5, 10, 20, 50, 100)
HISTORY_DAY_THRESHOLDS = (1, 3, 7, 14)
COMPLETED = "completed"
FAILED = "failed"


def missing_pct(missing: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(missing / total, 6)


def history_span_days(first: datetime | None, last: datetime | None) -> float | None:
    if first is None or last is None or last < first:
        return None
    return round((last - first).total_seconds() / 86400.0, 6)


def g3_exclusion_breakdown(
    rows: list[dict],
    *,
    min_confidence: float,
    min_completeness: float,
    high_threshold: float,
) -> dict:
    """Explain why G2 (or G1) rows are not G3. Does not change thresholds."""
    total = len(rows)
    g2 = [item for item in rows if item.get("in_g2")]
    g3 = [item for item in rows if item.get("in_g3")]
    reasons: Counter[str] = Counter()
    for item in g2:
        if item.get("in_g3"):
            continue
        if item.get("opportunity_status") != SCORED:
            reasons["not_scored"] += 1
        if item.get("ranking_label") != HIGH_OPPORTUNITY:
            reasons["not_high_opportunity"] += 1
        conf = item.get("confidence")
        if conf is None or conf < min_confidence:
            reasons["confidence_below_floor"] += 1
        comp = item.get("completeness")
        if comp is None or comp < min_completeness:
            reasons["completeness_below_floor"] += 1
        score = item.get("opportunity_score")
        if score is None or score < high_threshold:
            reasons["score_below_high_threshold"] += 1
    status_labels = Counter(
        (item.get("opportunity_status"), item.get("ranking_label")) for item in g2
    )
    return {
        "denominator": "point_in_time_candidates on the latest run (as-of-T reconstruction). Not live ranking.",
        "candidates": total,
        "g2_count": len(g2),
        "g3_count": len(g3),
        "g3_eligible": len(g3) > 0,
        "insufficient_g3": len(g3) < 30,
        "exclusion_reason_counts": dict(reasons),
        "g2_status_label_counts": {
            f"{status or 'none'}|{label or 'none'}": count for (status, label), count in status_labels.items()
        },
        "thresholds_frozen": {
            "min_confidence": min_confidence,
            "min_completeness": min_completeness,
            "high_opportunity": high_threshold,
        },
        "note": (
            "G3 requires G2 plus as-of-T SCORED and HIGH_OPPORTUNITY. "
            "Thresholds are not relaxed. Empty G3 is a coverage result, not a bug to patch by lowering floors."
        ),
    }


def _ordered(snaps: list[MarketSnapshot]) -> list[MarketSnapshot]:
    return sorted(
        [item for item in snaps if as_utc(item.recorded_at) is not None],
        key=lambda item: (as_utc(item.recorded_at), item.id or 0),
    )


def build_readiness_report(
    session: Session,
    settings: Settings | None = None,
    scheduler: DiscoveryScheduler | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    settings = settings or get_settings()
    scheduler = scheduler or get_scheduler()
    now = now or utc_now()
    tokens = list(session.scalars(select(Token)).all())
    snapshots = list(session.scalars(select(MarketSnapshot)).all())
    from app.data_protection import database_identity
    from app.services.sqlite_backup import last_successful_backup, latest_backup_status

    return assemble_readiness(
        tokens=tokens,
        snapshots=snapshots,
        discovery_scans=list(session.scalars(select(DiscoveryScan).order_by(DiscoveryScan.started_at.desc())).all()),
        risk_rows=list(session.scalars(select(TokenRiskAssessment)).all()),
        security_scans=list(session.scalars(select(TokenSecurityScan)).all()),
        narrative_matches=list(session.scalars(select(TokenNarrativeMatch)).all()),
        opportunities=list(session.scalars(select(OpportunityAssessment)).all()),
        historical_evals=list(session.scalars(select(HistoricalEvaluation).order_by(HistoricalEvaluation.id)).all()),
        quality_evals=list(
            session.scalars(select(QualityHistoricalEvaluation).order_by(QualityHistoricalEvaluation.id)).all()
        ),
        pit_run=session.scalar(select(PointInTimeRun).order_by(PointInTimeRun.id.desc()).limit(1)),
        pit_candidates=list(session.scalars(select(PointInTimeCandidate)).all()),
        settings=settings,
        scheduler=scheduler,
        now=now,
        database_identity=database_identity(settings.database_url),
        last_successful_backup=last_successful_backup(session),
        last_backup=latest_backup_status(session),
    )


def assemble_readiness(
    *,
    tokens: list[Token],
    snapshots: list[MarketSnapshot],
    discovery_scans: list[DiscoveryScan],
    risk_rows: list[TokenRiskAssessment],
    security_scans: list[TokenSecurityScan],
    narrative_matches: list[TokenNarrativeMatch],
    opportunities: list[OpportunityAssessment],
    historical_evals: list[HistoricalEvaluation],
    quality_evals: list[QualityHistoricalEvaluation],
    pit_run: PointInTimeRun | None,
    pit_candidates: list[PointInTimeCandidate],
    settings: Settings,
    scheduler: DiscoveryScheduler,
    now: datetime,
    database_identity: dict | None = None,
    last_successful_backup: dict | None = None,
    last_backup: dict | None = None,
) -> dict:
    by_token: dict[int, list[MarketSnapshot]] = defaultdict(list)
    for snap in snapshots:
        by_token[snap.token_id].append(snap)

    stamps = [as_utc(item.recorded_at) for item in snapshots if as_utc(item.recorded_at)]
    first_at = min(stamps) if stamps else None
    last_at = max(stamps) if stamps else None
    duration_days = history_span_days(first_at, last_at)
    token_n = len(tokens)
    snap_n = len(snapshots)

    obs_counts: dict[str, int] = {f"at_least_{n}": 0 for n in OBSERVATION_THRESHOLDS}
    day_counts: dict[str, int] = {f"at_least_{n}_days": 0 for n in HISTORY_DAY_THRESHOLDS}
    stale_hours = settings.quality_stale_hours
    stale_tokens = 0
    pair_change_tokens = 0
    pair_change_events = 0
    gaps: list[float] = []
    missing_liq = 0
    missing_val = 0
    liq_by_day: dict[str, dict[str, int]] = {}

    for token in tokens:
        ordered = _ordered(by_token.get(token.id, []))
        count = len(ordered)
        for threshold in OBSERVATION_THRESHOLDS:
            if count >= threshold:
                obs_counts[f"at_least_{threshold}"] += 1
        if ordered:
            span = history_span_days(as_utc(ordered[0].recorded_at), as_utc(ordered[-1].recorded_at))
            for threshold in HISTORY_DAY_THRESHOLDS:
                if span is not None and span >= threshold:
                    day_counts[f"at_least_{threshold}_days"] += 1
            last = as_utc(ordered[-1].recorded_at)
            if last is not None and now - last > timedelta(hours=stale_hours):
                stale_tokens += 1
            pairs = [(item.pair_address or "") for item in ordered]
            if len(set(pairs)) > 1:
                pair_change_tokens += 1
            pair_change_events += sum(1 for a, b in zip(pairs, pairs[1:]) if a != b)
            for prev, nxt in zip(ordered, ordered[1:]):
                start = as_utc(prev.recorded_at)
                end = as_utc(nxt.recorded_at)
                if start and end and end > start:
                    gaps.append((end - start).total_seconds() / 60.0)

    for snap in snapshots:
        if snap.liquidity_usd is None:
            missing_liq += 1
        if valuation_usd(snap) is None:
            missing_val += 1
        stamp = as_utc(snap.recorded_at)
        day = stamp.date().isoformat() if stamp else "unknown"
        bucket = liq_by_day.setdefault(day, {"total": 0, "missing_liquidity": 0, "missing_valuation": 0})
        bucket["total"] += 1
        if snap.liquidity_usd is None:
            bucket["missing_liquidity"] += 1
        if valuation_usd(snap) is None:
            bucket["missing_valuation"] += 1

    interval_buckets = {
        "le_5_min": sum(1 for gap in gaps if gap <= 5),
        "gt_5_le_20_min": sum(1 for gap in gaps if 5 < gap <= 20),
        "gt_20_le_40_min": sum(1 for gap in gaps if 20 < gap <= 40),
        "gt_40_le_120_min": sum(1 for gap in gaps if 40 < gap <= 120),
        "gt_120_min": sum(1 for gap in gaps if gap > 120),
    }

    tokens_with_risk = {row.token_id for row in risk_rows}
    tokens_with_security_scan = {row.token_id for row in security_scans}
    tokens_with_narrative = {row.token_id for row in narrative_matches}
    latest_opp: dict[int, OpportunityAssessment] = {}
    for row in sorted(opportunities, key=lambda item: (as_utc(item.assessed_at) or now, item.id or 0)):
        latest_opp[row.token_id] = row
    opp_labels = Counter(row.ranking_label for row in latest_opp.values())
    opp_status = Counter(row.status for row in latest_opp.values())

    pit_rows = pit_candidates
    if pit_run is not None:
        pit_rows = [item for item in pit_candidates if item.run_id == pit_run.id]
    g3 = g3_exclusion_breakdown(
        [
            {
                "in_g2": bool(item.in_g2),
                "in_g3": bool(item.in_g3),
                "opportunity_status": item.opportunity_status,
                "ranking_label": item.ranking_label,
                "confidence": item.confidence,
                "completeness": item.completeness,
                "opportunity_score": item.opportunity_score,
            }
            for item in pit_rows
        ],
        min_confidence=settings.opportunity_min_confidence,
        min_completeness=settings.opportunity_min_completeness,
        high_threshold=settings.opportunity_high_threshold,
    )
    g3["latest_point_in_time_run_id"] = pit_run.id if pit_run else None

    continuity = collection_continuity(
        discovery_scans,
        snapshots,
        expected_interval_minutes=settings.auto_discovery_interval_minutes,
        now=now,
    )
    snapshot_token_ids = {item.token_id for item in snapshots if item.token_id is not None}
    health = collection_health(
        discovery_scans,
        last_snapshot_at=last_at,
        scheduler=scheduler,
        expected_interval_minutes=settings.auto_discovery_interval_minutes,
        now=now,
        snapshot_count=snap_n,
        tokens_with_history=len(snapshot_token_ids),
        database_identity=database_identity,
        last_successful_backup=last_successful_backup,
        last_backup=last_backup,
        collection_gaps={
            "longest_gap_between_completed_scans_minutes": continuity.get(
                "longest_gap_between_completed_scans_minutes"
            ),
            "longest_gap": continuity.get("longest_gap"),
            "median_gap_between_completed_scans_minutes": continuity.get(
                "median_gap_between_completed_scans_minutes"
            ),
            "failed_scans_total": continuity.get("failed_scans_total"),
            "interrupted_scans_total": continuity.get("interrupted_scans_total"),
        },
    )

    return {
        "status": "ok",
        "generated_at": now.isoformat(),
        "paper_trading_only": True,
        "note": (
            "Read-only data-readiness report. Null liquidity is not treated as zero. "
            "Not a trading signal. Historical evaluation, quality, and point-in-time rows are not rewritten."
        ),
        "coverage": {
            "tokens": token_n,
            "snapshots": snap_n,
            "earliest_snapshot_at": first_at.isoformat() if first_at else None,
            "latest_snapshot_at": last_at.isoformat() if last_at else None,
            "collection_duration_days": duration_days,
            "denominator": "All stored tokens and market_snapshots.",
        },
        "observation_counts": {
            **obs_counts,
            "tokens_denominator": token_n,
            "note": "Counts of tokens with at least N timestamped snapshots.",
        },
        "history_span": {
            **day_counts,
            "tokens_denominator": token_n,
            "note": "Span is last_snapshot minus first_snapshot per token, in days.",
        },
        "snapshot_intervals": {
            "consecutive_gaps": len(gaps),
            "median_gap_minutes": round(median(gaps), 6) if gaps else None,
            "min_gap_minutes": round(min(gaps), 6) if gaps else None,
            "max_gap_minutes": round(max(gaps), 6) if gaps else None,
            "buckets": interval_buckets,
            "expected_interval_minutes": settings.auto_discovery_interval_minutes,
            "denominator": "Consecutive snapshot pairs per token with increasing timestamps.",
        },
        "liquidity": {
            "total_observations": snap_n,
            "missing": missing_liq,
            "non_null": snap_n - missing_liq,
            "missing_pct": missing_pct(missing_liq, snap_n),
            "by_utc_date": [
                {
                    "date": day,
                    "total": bucket["total"],
                    "missing_liquidity": bucket["missing_liquidity"],
                    "missing_pct": missing_pct(bucket["missing_liquidity"], bucket["total"]),
                }
                for day, bucket in sorted(liq_by_day.items())
            ],
            "note": "Missing liquidity stays null and is never treated as zero.",
        },
        "valuation": {
            "total_observations": snap_n,
            "missing": missing_val,
            "non_null": snap_n - missing_val,
            "missing_pct": missing_pct(missing_val, snap_n),
            "by_utc_date": [
                {
                    "date": day,
                    "total": bucket["total"],
                    "missing_valuation": bucket["missing_valuation"],
                    "missing_pct": missing_pct(bucket["missing_valuation"], bucket["total"]),
                }
                for day, bucket in sorted(liq_by_day.items())
            ],
            "note": "Missing means no usable market cap or FDV (same rule as historical evaluation).",
        },
        "stale_snapshots": {
            "tokens_with_last_print_older_than_hours": stale_hours,
            "stale_token_count": stale_tokens,
            "tokens_denominator": token_n,
            "stale_pct": missing_pct(stale_tokens, token_n),
            "as_of": now.isoformat(),
        },
        "pair_changes": {
            "tokens_with_multiple_pairs": pair_change_tokens,
            "consecutive_pair_change_events": pair_change_events,
            "tokens_denominator": token_n,
            "token_pct": missing_pct(pair_change_tokens, token_n),
        },
        "security": {
            "tokens_denominator": token_n,
            "tokens_with_risk_assessment": len(tokens_with_risk),
            "tokens_with_security_scan": len(tokens_with_security_scan),
            "risk_coverage_pct": missing_pct(len(tokens_with_risk), token_n),
            "scan_coverage_pct": missing_pct(len(tokens_with_security_scan), token_n),
            "note": "Coverage is presence of any stored row, not a safety rating.",
        },
        "narratives": {
            "tokens_denominator": token_n,
            "tokens_with_match": len(tokens_with_narrative),
            "match_rows": len(narrative_matches),
            "coverage_pct": missing_pct(len(tokens_with_narrative), token_n),
        },
        "opportunity_classifications": {
            "tokens_with_assessment": len(latest_opp),
            "tokens_denominator": token_n,
            "latest_status": dict(opp_status),
            "latest_ranking_label": dict(opp_labels),
            "scored_high_opportunity": sum(
                1
                for row in latest_opp.values()
                if row.status == SCORED and row.ranking_label == HIGH_OPPORTUNITY
            ),
            "note": "Latest stored opportunity_assessments only. These are not as-of-T G3 labels.",
        },
        "g3": g3,
        "collection_health": health,
        "collection_continuity": continuity,
        "preserved_records": {
            "historical_evaluations": [
                {"id": row.id, "horizon": row.horizon, "outcomes_measured": row.outcomes_measured}
                for row in historical_evals
            ],
            "quality_historical_evaluations": [
                {"id": row.id, "horizon": row.horizon, "outcomes_measured": row.outcomes_measured}
                for row in quality_evals
            ],
            "note": "Listed for comparison only. This report does not rewrite those rows.",
        },
        "repeat_phase6_when": {
            "min_collection_days": 14,
            "min_tokens_with_7_days": 100,
            "min_holdout_days_after_purge": 7,
            "g3_measured_holdout_min": 30,
            "note": (
                "Repeat Phase 6 after ~14 days of continuous 15-minute collection, "
                "with a 48h research / 90-minute purge / remaining holdout, and only if "
                "as-of-T SCORED+HIGH_OPPORTUNITY (G3) can populate without lowering floors."
            ),
        },
    }


def collection_health(
    discovery_scans: list[DiscoveryScan],
    *,
    last_snapshot_at: datetime | None,
    scheduler: DiscoveryScheduler,
    expected_interval_minutes: float,
    now: datetime,
    snapshot_count: int = 0,
    tokens_with_history: int = 0,
    database_identity: dict | None = None,
    last_successful_backup: dict | None = None,
    last_backup: dict | None = None,
    collection_gaps: dict | None = None,
) -> dict:
    ordered = sorted(discovery_scans, key=lambda item: as_utc(item.started_at) or now, reverse=True)
    last_success = next((item for item in ordered if item.status == COMPLETED), None)
    last_fail = next((item for item in ordered if item.status in FAILURE_STATUSES), None)
    consec = consecutive_failures([item.status for item in ordered])
    last_success_at = as_utc(last_success.finished_at or last_success.started_at) if last_success else None
    last_fail_at = as_utc(last_fail.finished_at or last_fail.started_at) if last_fail else None
    snapshot_age_minutes = None
    if last_snapshot_at is not None:
        snapshot_age_minutes = round(max((now - last_snapshot_at).total_seconds() / 60.0, 0), 6)
    expected = expected_interval_minutes
    recent = snapshot_age_minutes is not None and snapshot_age_minutes <= expected * 2
    actual_since_success = None
    if last_success_at is not None:
        actual_since_success = round(max((now - last_success_at).total_seconds() / 60.0, 0), 6)
    healthy = bool(
        scheduler.enabled
        and scheduler.running
        and recent
        and consec == 0
        and last_success is not None
    )
    reasons: list[str] = []
    if not scheduler.enabled:
        reasons.append("scheduler_disabled")
    if not scheduler.running:
        reasons.append("scheduler_not_running")
    if not recent:
        reasons.append("snapshots_not_recent")
    if consec:
        reasons.append("consecutive_discovery_failures")
    if last_success is None:
        reasons.append("no_completed_discovery_scan")
    return {
        "scheduler_enabled": scheduler.enabled,
        "scheduler_running": scheduler.running,
        "scan_in_progress": scheduler.is_busy(),
        "expected_interval_minutes": expected,
        "last_successful_discovery_scan_id": last_success.id if last_success else None,
        "last_successful_discovery_at": last_success_at.isoformat() if last_success_at else None,
        "last_failed_discovery_scan_id": last_fail.id if last_fail else None,
        "last_failed_discovery_at": last_fail_at.isoformat() if last_fail_at else None,
        "consecutive_scan_failures": consec,
        "minutes_since_last_completed_scan": actual_since_success,
        "latest_snapshot_at": last_snapshot_at.isoformat() if last_snapshot_at else None,
        "minutes_since_latest_snapshot": snapshot_age_minutes,
        "recent_snapshots": recent,
        "snapshot_count": snapshot_count,
        "tokens_with_history": tokens_with_history,
        "collection_gaps": collection_gaps or {},
        "database_identity": database_identity or {},
        "last_successful_backup": last_successful_backup,
        "last_backup": last_backup,
        "collection_healthy": healthy,
        "unhealthy_reasons": reasons,
        "note": (
            "Enabled is not healthy. Healthy requires the in-process scheduler running, "
            "a completed discovery scan, zero consecutive failures, and a snapshot no older "
            "than twice the expected interval. Scan totals come from discovery_scans. "
            "Process uptime is not stored. Backup failures are reported and never auto-restored."
        ),
    }
