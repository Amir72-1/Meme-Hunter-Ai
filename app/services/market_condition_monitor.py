"""Null-aware market and liquidity monitoring from stored DexScreener snapshots.

Read-only. Missing liquidity is never treated as zero. Findings are not rug,
honeypot, or unsellable labels and do not change opportunity scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DiscoveryScan, IntelligenceScan, MarketSnapshot, Token
from app.timeutil import as_utc, utc_now

VERIFIED_PRESENT = "VERIFIED_PRESENT"
VERIFIED_ABSENT = "VERIFIED_ABSENT"
UNKNOWN = "UNKNOWN"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

INFORMATIONAL = "INFORMATIONAL"
MARKET_CONDITION = "MARKET_CONDITION"
MARKET_FRESHNESS = "MARKET_FRESHNESS"
DATA_GAP = "DATA_GAP"

LIQUIDITY_DECREASE_40PCT = "LIQUIDITY_DECREASE_40PCT"
LIQUIDITY_INCREASE = "LIQUIDITY_INCREASE"
INSUFFICIENT_LIQUIDITY_EVIDENCE = "INSUFFICIENT_LIQUIDITY_EVIDENCE"
LIQUIDITY_TIME_GAP_EXCEEDED = "LIQUIDITY_TIME_GAP_EXCEEDED"
ZERO_LIQUIDITY = "ZERO_LIQUIDITY"
MARKET_STALE = "MARKET_STALE"
COLLECTOR_FAILURE = "COLLECTOR_FAILURE"
COLLECTOR_STOPPED = "COLLECTOR_STOPPED"
COLLECTOR_NO_NEW_SNAPSHOTS = "COLLECTOR_NO_NEW_SNAPSHOTS"
EXTREME_MARKET_CAP_CHANGE = "EXTREME_MARKET_CAP_CHANGE"
EXTREME_FDV_CHANGE = "EXTREME_FDV_CHANGE"
VALUATION_SOURCE_CHANGED = "VALUATION_SOURCE_CHANGED"
PAIR_ADDRESS_CHANGED = "PAIR_ADDRESS_CHANGED"
MISSING_LIQUIDITY_COVERAGE = "MISSING_LIQUIDITY_COVERAGE"
MISSING_VALUATION_COVERAGE = "MISSING_VALUATION_COVERAGE"
DUPLICATE_SNAPSHOT_TIMESTAMP = "DUPLICATE_SNAPSHOT_TIMESTAMP"

LIQUIDITY_DROP_RATIO = 0.40
MAX_COMPARE_GAP = timedelta(minutes=30)
STALE_30M = timedelta(minutes=30)
STALE_1H = timedelta(hours=1)
STALE_6H = timedelta(hours=6)
DEFAULT_SCAN_LIMIT = 25
MAX_SCAN_LIMIT = 50
SCAN_TYPE = "market_monitor"
MARKET_DATA_PROVIDER = "dexscreener"
DATA_SOURCE = MARKET_DATA_PROVIDER
COLLECTOR_HISTORY_LIMIT = 8
COLLECTOR_LOOKBACK = timedelta(hours=2)
CONSECUTIVE_FAIL_THRESHOLD = 3
CONSECUTIVE_ZERO_SNAPSHOT_THRESHOLD = 3
HEALTH_RUNNING = "running"
HEALTH_STOPPED = "stopped"
HEALTH_FAILING = "failing"
HEALTH_RUNNING_NO_SNAPSHOTS = "running_without_snapshots"
HEALTH_UNKNOWN = "unknown"

LIMIT_NO_RUG = (
    "This is a stored-print observation, not proof of a rug pull, honeypot, "
    "malicious developer, or inability to sell."
)


@dataclass
class MarketConditionFinding:
    code: str
    severity: str
    status: str
    token_address: str
    previous_observation_id: int | None = None
    current_observation_id: int | None = None
    previous_value: Any = None
    current_value: Any = None
    percentage_change: float | None = None
    previous_at: datetime | None = None
    current_at: datetime | None = None
    data_source: str = DATA_SOURCE
    market_data_provider: str = MARKET_DATA_PROVIDER
    discovery_source: str | None = None
    explanation: str = ""
    confidence: float = 0.0
    limitations: str = LIMIT_NO_RUG
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "status": self.status,
            "token_address": self.token_address,
            "previous_observation_id": self.previous_observation_id,
            "current_observation_id": self.current_observation_id,
            "previous_value": self.previous_value,
            "current_value": self.current_value,
            "percentage_change": self.percentage_change,
            "previous_at": self.previous_at.isoformat() if self.previous_at else None,
            "current_at": self.current_at.isoformat() if self.current_at else None,
            "data_source": self.data_source,
            "market_data_provider": self.market_data_provider,
            "discovery_source": self.discovery_source,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "limitations": self.limitations,
            "evidence": self.evidence,
        }


@dataclass
class AvailabilityCounts:
    missing: int
    total: int

    @property
    def missing_pct(self) -> float | None:
        if self.total <= 0:
            return None
        return round(self.missing / self.total, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "numerator_missing": self.missing,
            "denominator": self.total,
            "missing_pct": self.missing_pct,
            "note": "NULL is not imputed as zero.",
        }


@dataclass
class CollectorContext:
    latest_global_snapshot_at: datetime | None = None
    last_discovery_status: str | None = None
    last_discovery_finished_at: datetime | None = None
    recent_scans: list[dict[str, Any]] = field(default_factory=list)
    window_minutes: int = 120


@dataclass
class TokenMarketReport:
    token_address: str
    symbol: str | None
    signal_time: datetime
    findings: list[MarketConditionFinding]
    availability: dict[str, Any]
    snapshot_count: int
    note: str = (
        "Null-aware market monitoring of stored snapshots. "
        "Market-data completeness is separate from market risk. "
        + LIMIT_NO_RUG
    )


def snapshots_as_of(snapshots: list[MarketSnapshot], signal_time: datetime) -> list[MarketSnapshot]:
    cutoff = as_utc(signal_time)
    kept: list[MarketSnapshot] = []
    for item in snapshots:
        stamp = as_utc(item.recorded_at)
        if stamp is None or cutoff is None or stamp > cutoff:
            continue
        kept.append(item)
    kept.sort(key=lambda item: (as_utc(item.recorded_at) or cutoff, item.id or 0))
    return kept


def pct_change(previous: float | None, current: float | None) -> float | None:
    if previous is None or current is None:
        return None
    if previous <= 0:
        return None
    return (current - previous) / previous


def analyze_token_market_conditions(
    token_address: str,
    snapshots: list[MarketSnapshot],
    *,
    symbol: str | None = None,
    signal_time: datetime | None = None,
    collector: CollectorContext | None = None,
    extreme_multiple: float = 10.0,
) -> TokenMarketReport:
    now = as_utc(signal_time) or utc_now()
    ordered = snapshots_as_of(snapshots, now)
    findings: list[MarketConditionFinding] = []
    availability = _availability(ordered)
    findings.extend(_coverage_findings(token_address, availability, ordered))
    findings.extend(_duplicate_timestamp_findings(token_address, ordered))
    findings.extend(_liquidity_findings(token_address, ordered))
    findings.extend(_valuation_findings(token_address, ordered, extreme_multiple))
    findings.extend(_pair_findings(token_address, ordered))
    findings.extend(_freshness_findings(token_address, ordered, now, collector))
    return TokenMarketReport(
        token_address=token_address,
        symbol=symbol,
        signal_time=now,
        findings=findings,
        availability=availability,
        snapshot_count=len(ordered),
    )


def report_for_token(
    session: Session,
    token: Token,
    signal_time: datetime | None = None,
    settings: Settings | None = None,
) -> TokenMarketReport:
    settings = settings or get_settings()
    now = as_utc(signal_time) or utc_now()
    snaps = list(
        session.scalars(
            select(MarketSnapshot).where(MarketSnapshot.token_id == token.id)
        ).all()
    )
    return analyze_token_market_conditions(
        token.token_address,
        snaps,
        symbol=token.symbol,
        signal_time=now,
        collector=collector_context(session, now),
        extreme_multiple=settings.quality_extreme_move_multiple,
    )


def collector_context(session: Session, signal_time: datetime) -> CollectorContext:
    cutoff = as_utc(signal_time)
    latest = None
    for stamp in session.scalars(select(MarketSnapshot.recorded_at)).all():
        aware = as_utc(stamp)
        if aware is None or cutoff is None or aware > cutoff:
            continue
        if latest is None or aware > latest:
            latest = aware
    rows = list(
        session.scalars(select(DiscoveryScan).order_by(DiscoveryScan.id.desc()).limit(COLLECTOR_HISTORY_LIMIT)).all()
    )
    recent: list[dict[str, Any]] = []
    for scan in rows:
        started = as_utc(scan.started_at)
        finished = as_utc(scan.finished_at)
        if cutoff is not None:
            if started is not None and started > cutoff:
                continue
            if finished is not None and finished > cutoff:
                continue
        recent.append(
            {
                "id": scan.id,
                "status": scan.status,
                "started_at": started,
                "finished_at": finished,
                "snapshots_saved": scan.snapshots_saved or 0,
            }
        )
    last = recent[0] if recent else None
    return CollectorContext(
        latest_global_snapshot_at=latest,
        last_discovery_status=last["status"] if last else None,
        last_discovery_finished_at=last["finished_at"] if last else None,
        recent_scans=recent,
        window_minutes=int(COLLECTOR_LOOKBACK.total_seconds() // 60),
    )


def resolve_scan_limit(requested: int | None, *, token_address: str | None) -> tuple[int | None, int | None]:
    """Return (requested_limit, applied_limit). Single-token scans do not cap a list."""
    if token_address:
        return requested, None
    raw = DEFAULT_SCAN_LIMIT if requested is None else requested
    applied = min(max(int(raw), 1), MAX_SCAN_LIMIT)
    return raw, applied


def classify_collector_health(collector: CollectorContext, signal_time: datetime) -> dict[str, Any]:
    """Classify collector health from bounded scan history and global snapshot freshness."""
    now = as_utc(signal_time)
    window_start = now - COLLECTOR_LOOKBACK if now else None
    global_at = as_utc(collector.latest_global_snapshot_at)
    global_age = (now - global_at) if now and global_at else None
    global_fresh = global_age is not None and global_age <= STALE_30M
    scans = list(collector.recent_scans or [])
    if not scans and collector.last_discovery_status:
        scans = [
            {
                "id": None,
                "status": collector.last_discovery_status,
                "started_at": collector.last_discovery_finished_at,
                "finished_at": collector.last_discovery_finished_at,
                "snapshots_saved": 1 if global_fresh else 0,
            }
        ]
    in_window = []
    for scan in scans:
        stamp = as_utc(scan.get("finished_at") or scan.get("started_at"))
        if window_start is None or stamp is None or stamp >= window_start:
            in_window.append(scan)
    consecutive_failed = 0
    consecutive_zero = 0
    for scan in in_window:
        status = scan.get("status")
        saved = int(scan.get("snapshots_saved") or 0)
        if status == "failed" and consecutive_zero == 0:
            consecutive_failed += 1
            continue
        if status == "completed" and consecutive_failed == 0 and saved <= 0:
            consecutive_zero += 1
            continue
        break
    success_with_snaps = [
        scan
        for scan in in_window
        if scan.get("status") == "completed" and int(scan.get("snapshots_saved") or 0) > 0
    ]
    failed_in_window = sum(1 for scan in in_window if scan.get("status") == "failed")
    completed_in_window = sum(1 for scan in in_window if scan.get("status") == "completed")
    last = in_window[0] if in_window else None
    last_finished = as_utc(last.get("finished_at") if last else collector.last_discovery_finished_at)
    recent_scan = last_finished is not None and now is not None and now - last_finished <= STALE_30M
    recent_success_evidence = global_fresh or bool(success_with_snaps)
    last_status = last.get("status") if last else collector.last_discovery_status

    health = HEALTH_UNKNOWN
    if not in_window and global_at is None:
        health = HEALTH_UNKNOWN
    elif not recent_scan and not global_fresh:
        health = HEALTH_STOPPED
    elif consecutive_failed >= CONSECUTIVE_FAIL_THRESHOLD and not recent_success_evidence:
        health = HEALTH_FAILING
    elif consecutive_zero >= CONSECUTIVE_ZERO_SNAPSHOT_THRESHOLD and not global_fresh:
        health = HEALTH_RUNNING_NO_SNAPSHOTS
    elif recent_scan or global_fresh:
        health = HEALTH_RUNNING

    failure_present = health == HEALTH_FAILING
    stopped_present = health == HEALTH_STOPPED
    no_snaps_present = health == HEALTH_RUNNING_NO_SNAPSHOTS
    if consecutive_failed == 1 and recent_success_evidence:
        failure_present = False
    if consecutive_failed >= CONSECUTIVE_FAIL_THRESHOLD and recent_success_evidence:
        failure_present = False
    if consecutive_zero == 1:
        no_snaps_present = False
    if consecutive_zero >= CONSECUTIVE_ZERO_SNAPSHOT_THRESHOLD and not global_fresh and recent_scan:
        no_snaps_present = True
        if health == HEALTH_RUNNING:
            health = HEALTH_RUNNING_NO_SNAPSHOTS

    return {
        "health": health,
        "window_minutes": collector.window_minutes,
        "window_start": window_start.isoformat() if window_start else None,
        "global_snapshot_at": global_at.isoformat() if global_at else None,
        "global_age_minutes": None if global_age is None else round(global_age.total_seconds() / 60, 4),
        "global_fresh": global_fresh,
        "recent_scan": recent_scan,
        "last_status": last_status,
        "last_finished_at": last_finished.isoformat() if last_finished else None,
        "scans_in_window": len(in_window),
        "completed_in_window": completed_in_window,
        "failed_in_window": failed_in_window,
        "consecutive_failed": consecutive_failed,
        "consecutive_zero_snapshot_success": consecutive_zero,
        "success_with_snapshots": len(success_with_snaps),
        "recent_success_evidence": recent_success_evidence,
        "scan_ids": [scan.get("id") for scan in in_window],
        "failure_status": (
            VERIFIED_PRESENT if failure_present else VERIFIED_ABSENT if health != HEALTH_UNKNOWN else UNKNOWN
        ),
        "stopped_status": (
            VERIFIED_PRESENT if stopped_present else VERIFIED_ABSENT if health != HEALTH_UNKNOWN else UNKNOWN
        ),
        "no_snapshots_status": (
            VERIFIED_PRESENT
            if no_snaps_present
            else VERIFIED_ABSENT
            if health != HEALTH_UNKNOWN
            else UNKNOWN
        ),
    }


def run_market_monitor_scan(
    session: Session,
    settings: Settings | None = None,
    *,
    token_address: str | None = None,
    limit: int | None = None,
    signal_time: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = as_utc(signal_time) or utc_now()
    started = utc_now()
    scan_row = IntelligenceScan(scan_type=SCAN_TYPE, started_at=started, status="running")
    session.add(scan_row)
    session.commit()
    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    requested_limit, applied_limit = resolve_scan_limit(limit, token_address=token_address)
    if applied_limit is not None:
        tokens = tokens[:applied_limit]
    items = []
    decrease_count = 0
    stale_count = 0
    for token in tokens:
        report = report_for_token(session, token, now, settings)
        if any(item.code == LIQUIDITY_DECREASE_40PCT and item.status == VERIFIED_PRESENT for item in report.findings):
            decrease_count += 1
        if any(item.code == MARKET_STALE and item.status == VERIFIED_PRESENT for item in report.findings):
            stale_count += 1
        items.append(_report_payload(report))
    finished = utc_now()
    payload = {
        "scan_id": scan_row.id,
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "signal_time": now.isoformat(),
        "tokens_scanned": len(tokens),
        "requested_limit": requested_limit,
        "applied_limit": applied_limit,
        "max_scan_limit": MAX_SCAN_LIMIT,
        "liquidity_decreases": decrease_count,
        "stale_tokens": stale_count,
        "thresholds": {
            "liquidity_drop_ratio": LIQUIDITY_DROP_RATIO,
            "max_compare_gap_minutes": MAX_COMPARE_GAP.total_seconds() / 60,
            "stale_minutes": [30, 60, 360],
            "extreme_valuation_multiple": settings.quality_extreme_move_multiple,
            "requested_limit": requested_limit,
            "applied_limit": applied_limit,
            "max_scan_limit": MAX_SCAN_LIMIT,
        },
        "items": items,
        "note": (
            "Read-only snapshot monitor. Does not lock the discovery scheduler. "
            + LIMIT_NO_RUG
        ),
    }
    scan_row.status = "completed"
    scan_row.finished_at = finished
    scan_row.items_fetched = len(tokens)
    scan_row.items_saved = len(tokens)
    scan_row.details_json = json.dumps(payload)
    session.commit()
    return payload


def latest_monitor_scan(session: Session) -> IntelligenceScan | None:
    return session.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == SCAN_TYPE)
        .order_by(IntelligenceScan.id.desc())
        .limit(1)
    )


def finding_by_code(findings: list[MarketConditionFinding], code: str) -> MarketConditionFinding | None:
    for item in findings:
        if item.code == code:
            return item
    return None


def _report_payload(report: TokenMarketReport) -> dict[str, Any]:
    return {
        "token_address": report.token_address,
        "symbol": report.symbol,
        "signal_time": report.signal_time.isoformat(),
        "snapshot_count": report.snapshot_count,
        "availability": report.availability,
        "findings": [item.as_dict() for item in report.findings],
        "note": report.note,
    }


def _availability(ordered: list[MarketSnapshot]) -> dict[str, Any]:
    total = len(ordered)
    liq_missing = sum(1 for item in ordered if item.liquidity_usd is None)
    mcap_missing = sum(1 for item in ordered if item.market_cap is None)
    fdv_missing = sum(1 for item in ordered if item.fdv is None)
    valuation_missing = sum(1 for item in ordered if item.market_cap is None and item.fdv is None)
    return {
        "liquidity": AvailabilityCounts(liq_missing, total).as_dict(),
        "market_cap": AvailabilityCounts(mcap_missing, total).as_dict(),
        "fdv": AvailabilityCounts(fdv_missing, total).as_dict(),
        "valuation_any": AvailabilityCounts(valuation_missing, total).as_dict(),
        "note": "Market-data completeness is separate from market risk. NULL is not zero.",
    }


def _coverage_findings(
    token_address: str,
    availability: dict[str, Any],
    ordered: list[MarketSnapshot],
) -> list[MarketConditionFinding]:
    current = ordered[-1] if ordered else None
    liq = availability["liquidity"]
    val = availability["valuation_any"]
    findings = [
        MarketConditionFinding(
            code=MISSING_LIQUIDITY_COVERAGE,
            severity=DATA_GAP,
            status=VERIFIED_PRESENT if (liq["numerator_missing"] or 0) > 0 else VERIFIED_ABSENT,
            token_address=token_address,
            current_observation_id=current.id if current else None,
            current_at=as_utc(current.recorded_at) if current else None,
            current_value=liq["numerator_missing"],
            **_prov(current),
            explanation=(
                f"{liq['numerator_missing']}/{liq['denominator']} snapshots have NULL liquidity "
                f"({_pct_label(liq['missing_pct'])} missing). NULL is not treated as zero."
            ),
            confidence=1.0 if ordered else 0.0,
            evidence=liq,
            limitations="Missing liquidity coverage is completeness, not a rug-pull conclusion.",
        ),
        MarketConditionFinding(
            code=MISSING_VALUATION_COVERAGE,
            severity=DATA_GAP,
            status=VERIFIED_PRESENT if (val["numerator_missing"] or 0) > 0 else VERIFIED_ABSENT,
            token_address=token_address,
            current_observation_id=current.id if current else None,
            current_at=as_utc(current.recorded_at) if current else None,
            current_value=val["numerator_missing"],
            **_prov(current),
            explanation=(
                f"{val['numerator_missing']}/{val['denominator']} snapshots have neither market cap nor FDV "
                f"({_pct_label(val['missing_pct'])} missing). Sources are not combined."
            ),
            confidence=1.0 if ordered else 0.0,
            evidence={"valuation_any": val, "market_cap": availability["market_cap"], "fdv": availability["fdv"]},
            limitations="Missing valuation is completeness, not a price forecast.",
        ),
    ]
    if not ordered:
        for item in findings:
            item.status = INSUFFICIENT_DATA
            item.confidence = 0.0
            item.explanation = "No snapshots at or before signal time."
    return findings


def _duplicate_timestamp_findings(token_address: str, ordered: list[MarketSnapshot]) -> list[MarketConditionFinding]:
    findings: list[MarketConditionFinding] = []
    for prev, nxt in zip(ordered, ordered[1:]):
        prev_at = as_utc(prev.recorded_at)
        nxt_at = as_utc(nxt.recorded_at)
        if prev_at is None or nxt_at is None or prev_at != nxt_at:
            continue
        findings.append(
            MarketConditionFinding(
                code=DUPLICATE_SNAPSHOT_TIMESTAMP,
                severity=INFORMATIONAL,
                status=VERIFIED_PRESENT,
                token_address=token_address,
                previous_observation_id=prev.id,
                current_observation_id=nxt.id,
                previous_at=prev_at,
                current_at=nxt_at,
                explanation="Consecutive snapshots share the same recorded_at. Ordering uses snapshot id.",
                confidence=1.0,
                evidence={"previous_id": prev.id, "current_id": nxt.id},
                limitations="Duplicate timestamps are a data quirk, not proof of manipulation.",
            )
        )
    return findings


def _liquidity_findings(token_address: str, ordered: list[MarketSnapshot]) -> list[MarketConditionFinding]:
    if not ordered:
        return [
            MarketConditionFinding(
                code=INSUFFICIENT_LIQUIDITY_EVIDENCE,
                severity=DATA_GAP,
                status=INSUFFICIENT_DATA,
                token_address=token_address,
                explanation="No snapshot is available at or before signal time.",
                limitations="Missing prints are not treated as zero liquidity.",
            )
        ]
    current = ordered[-1]
    current_liq = current.liquidity_usd
    previous = _previous_non_null_liquidity(ordered[:-1])
    findings: list[MarketConditionFinding] = []

    if current_liq == 0:
        findings.append(
            MarketConditionFinding(
                code=ZERO_LIQUIDITY,
                severity=MARKET_CONDITION,
                status=VERIFIED_PRESENT,
                token_address=token_address,
                current_observation_id=current.id,
                current_value=0.0,
                current_at=as_utc(current.recorded_at),
                **_prov(current),
                explanation="Current snapshot records liquidity_usd=0. This is distinct from NULL liquidity.",
                confidence=1.0,
                evidence={"liquidity_usd": 0.0, "is_null": False},
            )
        )

    if current_liq is None:
        findings.append(
            _liq_gap(
                token_address,
                current,
                previous,
                "Current liquidity is NULL. Missing liquidity is not treated as zero.",
            )
        )
        return findings
    if previous is None:
        findings.append(
            _liq_gap(
                token_address,
                current,
                None,
                "No previous non-null liquidity observation exists at or before signal time.",
            )
        )
        return findings

    prev_at = as_utc(previous.recorded_at)
    curr_at = as_utc(current.recorded_at)
    gap = (curr_at - prev_at) if prev_at and curr_at else None
    if gap is None or gap > MAX_COMPARE_GAP:
        minutes = None if gap is None else round(gap.total_seconds() / 60, 4)
        findings.append(
            MarketConditionFinding(
                code=LIQUIDITY_TIME_GAP_EXCEEDED,
                severity=DATA_GAP,
                status=INSUFFICIENT_DATA,
                token_address=token_address,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                previous_value=previous.liquidity_usd,
                current_value=current_liq,
                previous_at=prev_at,
                current_at=curr_at,
                **_prov(current),
                explanation=(
                    f"Previous non-null liquidity is more than 30 minutes before the current print "
                    f"(gap_minutes={minutes}). No 40% decrease is claimed."
                ),
                evidence={"gap_minutes": minutes, "max_gap_minutes": 30},
                limitations="Wide gaps are not filled with later or imputed values.",
            )
        )
        return findings

    change = pct_change(previous.liquidity_usd, current_liq)
    if change is None:
        findings.append(
            _liq_gap(
                token_address,
                current,
                previous,
                "Percentage change cannot be computed because previous liquidity is not a positive value.",
            )
        )
        return findings

    drop = MarketConditionFinding(
        code=LIQUIDITY_DECREASE_40PCT,
        severity=MARKET_CONDITION,
        status=VERIFIED_PRESENT if change <= -LIQUIDITY_DROP_RATIO else VERIFIED_ABSENT,
        token_address=token_address,
        previous_observation_id=previous.id,
        current_observation_id=current.id,
        previous_value=previous.liquidity_usd,
        current_value=current_liq,
        percentage_change=round(change, 6),
        previous_at=prev_at,
        current_at=curr_at,
        **_prov(current),
        explanation=(
            f"Liquidity changed {change:.2%} versus the previous non-null print within 30 minutes."
            if change > -LIQUIDITY_DROP_RATIO
            else f"Liquidity decreased {abs(change):.2%} versus the previous non-null print within 30 minutes."
        ),
        confidence=1.0,
        evidence={
            "previous_id": previous.id,
            "current_id": current.id,
            "threshold": LIQUIDITY_DROP_RATIO,
            "gap_seconds": gap.total_seconds(),
        },
    )
    findings.append(drop)
    if change > 0:
        findings.append(
            MarketConditionFinding(
                code=LIQUIDITY_INCREASE,
                severity=INFORMATIONAL,
                status=VERIFIED_PRESENT,
                token_address=token_address,
                previous_observation_id=previous.id,
                current_observation_id=current.id,
                previous_value=previous.liquidity_usd,
                current_value=current_liq,
                percentage_change=round(change, 6),
                previous_at=prev_at,
                current_at=curr_at,
                **_prov(current),
                explanation=f"Liquidity increased {change:.2%} versus the previous non-null print.",
                confidence=1.0,
                limitations="An increase is not a buy signal.",
            )
        )
    return findings


def _valuation_findings(
    token_address: str,
    ordered: list[MarketSnapshot],
    extreme_multiple: float,
) -> list[MarketConditionFinding]:
    if len(ordered) < 2:
        return []
    prev, current = ordered[-2], ordered[-1]
    findings: list[MarketConditionFinding] = []
    prev_source = _explicit_source(prev)
    curr_source = _explicit_source(current)
    if prev_source and curr_source and prev_source != curr_source:
        findings.append(
            MarketConditionFinding(
                code=VALUATION_SOURCE_CHANGED,
                severity=INFORMATIONAL,
                status=VERIFIED_PRESENT,
                token_address=token_address,
                previous_observation_id=prev.id,
                current_observation_id=current.id,
                previous_value=prev_source,
                current_value=curr_source,
                previous_at=as_utc(prev.recorded_at),
                current_at=as_utc(current.recorded_at),
                **_prov(current),
                explanation=(
                    f"Valuation source switched from {prev_source} to {curr_source}. "
                    "Sources are not combined into one return."
                ),
                confidence=1.0,
                evidence={
                    "previous_market_cap": prev.market_cap,
                    "previous_fdv": prev.fdv,
                    "current_market_cap": current.market_cap,
                    "current_fdv": current.fdv,
                },
                limitations="A source switch is not a trade and is not mixed into a single percentage.",
            )
        )
    elif prev_source and curr_source and prev_source == curr_source:
        findings.append(
            MarketConditionFinding(
                code=VALUATION_SOURCE_CHANGED,
                severity=INFORMATIONAL,
                status=VERIFIED_ABSENT,
                token_address=token_address,
                previous_observation_id=prev.id,
                current_observation_id=current.id,
                previous_value=prev_source,
                current_value=curr_source,
                previous_at=as_utc(prev.recorded_at),
                current_at=as_utc(current.recorded_at),
                explanation=f"Consecutive snapshots use the same valuation source ({prev_source}).",
                confidence=1.0,
                limitations="Same-source prints are still DexScreener quotes, not executed trades.",
            )
        )

    findings.extend(
        _extreme_field(
            token_address,
            prev,
            current,
            field_name="market_cap",
            code=EXTREME_MARKET_CAP_CHANGE,
            multiple=extreme_multiple,
        )
    )
    findings.extend(
        _extreme_field(
            token_address,
            prev,
            current,
            field_name="fdv",
            code=EXTREME_FDV_CHANGE,
            multiple=extreme_multiple,
        )
    )
    return findings


def _extreme_field(
    token_address: str,
    prev: MarketSnapshot,
    current: MarketSnapshot,
    *,
    field_name: str,
    code: str,
    multiple: float,
) -> list[MarketConditionFinding]:
    left = getattr(prev, field_name)
    right = getattr(current, field_name)
    if left is None or right is None:
        return []
    change = pct_change(float(left), float(right))
    if change is None:
        return []
    ratio = float(right) / float(left) if left else None
    extreme = ratio is not None and (ratio >= multiple or (ratio > 0 and ratio <= 1 / multiple))
    if not extreme:
        return []
    return [
        MarketConditionFinding(
            code=code,
            severity=MARKET_CONDITION,
            status=VERIFIED_PRESENT,
            token_address=token_address,
            previous_observation_id=prev.id,
            current_observation_id=current.id,
            previous_value=float(left),
            current_value=float(right),
            percentage_change=round(change, 6),
            previous_at=as_utc(prev.recorded_at),
            current_at=as_utc(current.recorded_at),
            **_prov(current),
            explanation=(
                f"{field_name} changed {change:.2%} on the same source. "
                "This is an extreme stored-print move, not a proven executable trade."
            ),
            confidence=1.0,
            evidence={"field": field_name, "ratio": ratio, "multiple": multiple},
        )
    ]


def _pair_findings(token_address: str, ordered: list[MarketSnapshot]) -> list[MarketConditionFinding]:
    if len(ordered) < 2:
        return []
    prev, current = ordered[-2], ordered[-1]
    old_pair = prev.pair_address
    new_pair = current.pair_address
    changed = (old_pair or "") != (new_pair or "")
    return [
        MarketConditionFinding(
            code=PAIR_ADDRESS_CHANGED,
            severity=INFORMATIONAL,
            status=VERIFIED_PRESENT if changed else VERIFIED_ABSENT,
            token_address=token_address,
            previous_observation_id=prev.id,
            current_observation_id=current.id,
            previous_value=old_pair,
            current_value=new_pair,
            previous_at=as_utc(prev.recorded_at),
            current_at=as_utc(current.recorded_at),
            **_prov(current),
            explanation=(
                f"Pair address changed from {old_pair!r} to {new_pair!r}."
                if changed
                else "Pair address is unchanged between the latest two snapshots."
            ),
            confidence=1.0,
            limitations="A pair change is not assumed to be malicious.",
        )
    ]


def _freshness_findings(
    token_address: str,
    ordered: list[MarketSnapshot],
    signal_time: datetime,
    collector: CollectorContext | None,
) -> list[MarketConditionFinding]:
    if not ordered:
        empty = [
            MarketConditionFinding(
                code=MARKET_STALE,
                severity=MARKET_FRESHNESS,
                status=INSUFFICIENT_DATA,
                token_address=token_address,
                explanation="No snapshot exists at or before signal time, so staleness cannot be measured.",
                limitations="A token with no snapshot is not automatically a collector failure.",
                **_prov(None),
            )
        ]
        empty.extend(_collector_findings(token_address, None, collector, signal_time, **_prov(None)))
        return empty
    current = ordered[-1]
    current_at = as_utc(current.recorded_at)
    age = (signal_time - current_at) if current_at else None
    crossed = []
    if age is not None:
        if age > STALE_30M:
            crossed.append("30m")
        if age > STALE_1H:
            crossed.append("1h")
        if age > STALE_6H:
            crossed.append("6h")
    stale_status = VERIFIED_PRESENT if crossed else VERIFIED_ABSENT
    age_minutes = None if age is None else round(age.total_seconds() / 60, 4)
    token_stale = MarketConditionFinding(
        code=MARKET_STALE,
        severity=MARKET_FRESHNESS,
        status=stale_status,
        token_address=token_address,
        current_observation_id=current.id,
        current_at=current_at,
        current_value=age_minutes,
        **_prov(current),
        explanation=(
            f"Latest token snapshot is {age_minutes} minutes old at signal time. Thresholds crossed: {crossed or ['none']}."
        ),
        confidence=1.0,
        evidence={
            "age_minutes": age_minutes,
            "thresholds_crossed": crossed,
            "scope": "token",
        },
        limitations="A stale token print does not mean the collector stopped.",
    )
    findings = [token_stale]
    findings.extend(_collector_findings(token_address, current_at, collector, signal_time, **_prov(current)))
    return findings


def _collector_findings(
    token_address: str,
    token_latest_at: datetime | None,
    collector: CollectorContext | None,
    signal_time: datetime,
    **provenance: Any,
) -> list[MarketConditionFinding]:
    if collector is None:
        return [
            MarketConditionFinding(
                code=code,
                severity=MARKET_FRESHNESS,
                status=UNKNOWN,
                token_address=token_address,
                explanation="Collector context was not provided; collector health is unknown.",
                confidence=0.0,
                limitations="Token-level staleness is distinct from collector health.",
                **provenance,
            )
            for code in (COLLECTOR_FAILURE, COLLECTOR_STOPPED, COLLECTOR_NO_NEW_SNAPSHOTS)
        ]
    classified = classify_collector_health(collector, signal_time)
    evidence = {**classified, "token_latest_at": token_latest_at.isoformat() if token_latest_at else None, "scope": "collector"}
    health = classified["health"]
    specs = [
        (
            COLLECTOR_FAILURE,
            classified["failure_status"],
            "Repeated failed discovery scans with no recent successful collection evidence."
            if classified["failure_status"] == VERIFIED_PRESENT
            else "Collector failure is not indicated. One failed scan is not treated as collector failure when recent collection evidence exists.",
        ),
        (
            COLLECTOR_STOPPED,
            classified["stopped_status"],
            "No recent discovery scans and no fresh global snapshots; the collector appears stopped."
            if classified["stopped_status"] == VERIFIED_PRESENT
            else "The collector is not classified as stopped on this window.",
        ),
        (
            COLLECTOR_NO_NEW_SNAPSHOTS,
            classified["no_snapshots_status"],
            "Discovery scans are still running but are not producing new snapshots."
            if classified["no_snapshots_status"] == VERIFIED_PRESENT
            else "Running-without-snapshots is not indicated. A single zero-snapshot scan is not treated as collector failure.",
        ),
    ]
    findings: list[MarketConditionFinding] = []
    for code, status, explanation in specs:
        findings.append(
            MarketConditionFinding(
                code=code,
                severity=MARKET_FRESHNESS,
                status=status,
                token_address=token_address,
                current_at=as_utc(collector.latest_global_snapshot_at),
                current_value=health,
                explanation=explanation,
                confidence=1.0 if status != UNKNOWN else 0.0,
                evidence=dict(evidence),
                limitations="Token-level staleness is distinct from collector health. Classification uses a bounded scan history.",
                **provenance,
            )
        )
    return findings


def _previous_non_null_liquidity(prior: list[MarketSnapshot]) -> MarketSnapshot | None:
    for item in reversed(prior):
        if item.liquidity_usd is not None:
            return item
    return None


def _liq_gap(
    token_address: str,
    current: MarketSnapshot,
    previous: MarketSnapshot | None,
    explanation: str,
) -> MarketConditionFinding:
    return MarketConditionFinding(
        code=INSUFFICIENT_LIQUIDITY_EVIDENCE,
        severity=DATA_GAP,
        status=INSUFFICIENT_DATA,
        token_address=token_address,
        previous_observation_id=previous.id if previous else None,
        current_observation_id=current.id,
        previous_value=None if previous is None else previous.liquidity_usd,
        current_value=current.liquidity_usd,
        previous_at=as_utc(previous.recorded_at) if previous else None,
        current_at=as_utc(current.recorded_at),
        **_prov(current),
        explanation=explanation,
        evidence={"current_is_null": current.liquidity_usd is None},
        limitations="NULL liquidity is not treated as zero and is not a 40% decrease.",
    )


def _explicit_source(snapshot: MarketSnapshot) -> str | None:
    has_mcap = snapshot.market_cap is not None
    has_fdv = snapshot.fdv is not None
    if has_mcap and not has_fdv:
        return "market_cap"
    if has_fdv and not has_mcap:
        return "fdv"
    if has_mcap and has_fdv:
        return "market_cap"
    return None


def _prov(snapshot: MarketSnapshot | None = None) -> dict[str, Any]:
    discovery = snapshot.discovery_source if snapshot is not None else None
    return {
        "data_source": MARKET_DATA_PROVIDER,
        "market_data_provider": MARKET_DATA_PROVIDER,
        "discovery_source": discovery,
    }


def _pct_label(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2%}"
