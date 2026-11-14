"""Startup recovery and collection-continuity helpers. Does not rewrite snapshots."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DiscoveryScan, MarketSnapshot
from app.timeutil import as_utc, utc_now

logger = logging.getLogger("meme_hunter.discovery")

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"
SKIPPED = "skipped"
FAILURE_STATUSES = {FAILED, INTERRUPTED}


def consecutive_failures(statuses: list[str]) -> int:
    """`statuses` newest-first. Skip in-progress/skipped. Stop at first completed."""
    count = 0
    for status in statuses:
        if status in {COMPLETED, "success"}:
            return count
        if status in {RUNNING, SKIPPED}:
            continue
        if status in FAILURE_STATUSES:
            count += 1
    return count


def recover_abandoned_scans(session: Session, *, now: datetime | None = None) -> list[int]:
    """Mark leftover `running` rows as interrupted. Idempotent. Does not touch completed/failed."""

    now = now or utc_now()
    rows = list(session.scalars(select(DiscoveryScan).where(DiscoveryScan.status == RUNNING)).all())
    recovered: list[int] = []
    for row in rows:
        detail = (
            "Scan left in running after a process restart, crash, or shutdown. "
            "Uncommitted snapshots from that attempt were not written. "
            "Prior committed snapshots were not modified."
        )
        payload = [
            {
                "stage": "startup_recovery",
                "detail": detail[:500],
                "token_address": None,
                "category": "interrupted",
            }
        ]
        existing = []
        if row.errors_json:
            try:
                loaded = json.loads(row.errors_json)
                if isinstance(loaded, list):
                    existing = loaded
            except json.JSONDecodeError:
                existing = []
        row.status = INTERRUPTED
        row.finished_at = now
        row.errors_json = json.dumps(existing + payload)
        recovered.append(row.id)
        logger.warning(
            "Recovered abandoned discovery scan id=%s stage=startup_recovery category=interrupted "
            "duration_s=unknown retryable=true",
            row.id,
        )
    if recovered:
        session.commit()
    return recovered


def last_completed_scan(session: Session) -> DiscoveryScan | None:
    return session.scalar(
        select(DiscoveryScan)
        .where(DiscoveryScan.status == COMPLETED)
        .order_by(DiscoveryScan.finished_at.desc(), DiscoveryScan.id.desc())
        .limit(1)
    )


def scan_is_overdue(
    scan: DiscoveryScan | None,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a prior successful scan is older than the collection interval. First start is not overdue."""

    if scan is None:
        return False
    now = now or utc_now()
    stamp = as_utc(scan.finished_at) or as_utc(scan.started_at)
    if stamp is None:
        return False
    return now - stamp >= timedelta(seconds=settings.auto_discovery_interval_seconds)


def scan_duration_seconds(scan: DiscoveryScan) -> float | None:
    start = as_utc(scan.started_at)
    end = as_utc(scan.finished_at)
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 6)


def persistent_scheduler_stats(session: Session, *, now: datetime | None = None) -> dict:
    now = now or utc_now()
    rows = list(session.scalars(select(DiscoveryScan).order_by(DiscoveryScan.started_at.desc())).all())
    completed = [row for row in rows if row.status == COMPLETED]
    failed = [row for row in rows if row.status == FAILED]
    interrupted = [row for row in rows if row.status == INTERRUPTED]
    last_success = completed[0] if completed else None
    failure_rows = [row for row in rows if row.status in FAILURE_STATUSES]
    last_failure = failure_rows[0] if failure_rows else None
    latest = rows[0] if rows else None
    last_duration = scan_duration_seconds(latest) if latest else None
    last_success_at = (
        (as_utc(last_success.finished_at) or as_utc(last_success.started_at)) if last_success else None
    )
    last_fail_at = (
        (as_utc(last_failure.finished_at) or as_utc(last_failure.started_at)) if last_failure else None
    )
    return {
        "completed_scans_total": len(completed),
        "failed_scans_total": len(failed) + len(interrupted),
        "interrupted_scans_total": len(interrupted),
        "failed_only_total": len(failed),
        "last_successful_scan_id": last_success.id if last_success else None,
        "last_successful_scan_at": last_success_at.isoformat() if last_success_at else None,
        "last_failed_scan_id": last_failure.id if last_failure else None,
        "last_failed_scan_at": last_fail_at.isoformat() if last_fail_at else None,
        "last_scan_status": latest.status if latest else "never",
        "last_scan_duration_seconds": last_duration,
        "consecutive_scan_failures": consecutive_failures([row.status for row in rows]),
        "persistent_last_started_at": as_utc(latest.started_at).isoformat() if latest and as_utc(latest.started_at) else None,
        "persistent_last_finished_at": as_utc(latest.finished_at).isoformat()
        if latest and as_utc(latest.finished_at)
        else None,
    }


def collection_continuity(
    scans: list[DiscoveryScan],
    snapshots: list[MarketSnapshot],
    *,
    expected_interval_minutes: float,
    now: datetime,
) -> dict:
    completed = sorted(
        [row for row in scans if row.status == COMPLETED and as_utc(row.started_at)],
        key=lambda row: as_utc(row.started_at) or now,
    )
    failed_n = sum(1 for row in scans if row.status == FAILED)
    interrupted_n = sum(1 for row in scans if row.status == INTERRUPTED)
    execution = 0.0
    execution_n = 0
    for row in scans:
        duration = scan_duration_seconds(row)
        if duration is not None:
            execution += duration
            execution_n += 1

    gaps: list[float] = []
    longest_gap = None
    longest_pair: dict | None = None
    for prev, nxt in zip(completed, completed[1:]):
        start = as_utc(prev.finished_at) or as_utc(prev.started_at)
        end = as_utc(nxt.started_at)
        if start and end and end > start:
            minutes = (end - start).total_seconds() / 60.0
            gaps.append(minutes)
            if longest_gap is None or minutes > longest_gap:
                longest_gap = minutes
                longest_pair = {
                    "after_scan_id": prev.id,
                    "before_scan_id": nxt.id,
                    "gap_minutes": round(minutes, 6),
                }

    snap_stamps = [as_utc(item.recorded_at) for item in snapshots if as_utc(item.recorded_at)]
    first_snap = min(snap_stamps) if snap_stamps else None
    last_snap = max(snap_stamps) if snap_stamps else None
    calendar_days = None
    if first_snap and last_snap:
        calendar_days = round((last_snap - first_snap).total_seconds() / 86400.0, 6)

    scans_by_day: dict[str, int] = defaultdict(int)
    for row in completed:
        stamp = as_utc(row.finished_at) or as_utc(row.started_at)
        if stamp:
            scans_by_day[stamp.date().isoformat()] += 1
    snaps_by_day: dict[str, int] = defaultdict(int)
    for stamp in snap_stamps:
        snaps_by_day[stamp.date().isoformat()] += 1

    median_gap = None
    if gaps:
        ordered_gaps = sorted(gaps)
        mid = len(ordered_gaps) // 2
        if len(ordered_gaps) % 2:
            median_gap = round(ordered_gaps[mid], 6)
        else:
            median_gap = round((ordered_gaps[mid - 1] + ordered_gaps[mid]) / 2.0, 6)

    snapshot_age_minutes = None
    if last_snap is not None:
        snapshot_age_minutes = round(max((now - last_snap).total_seconds() / 60.0, 0.0), 6)

    expected = expected_interval_minutes
    return {
        "calendar_span_days": calendar_days,
        "earliest_snapshot_at": first_snap.isoformat() if first_snap else None,
        "latest_snapshot_at": last_snap.isoformat() if last_snap else None,
        "latest_snapshot_age_minutes": snapshot_age_minutes,
        "accumulated_scan_execution_seconds": round(execution, 6),
        "scans_with_duration": execution_n,
        "completed_scans_total": len(completed),
        "failed_scans_total": failed_n + interrupted_n,
        "interrupted_scans_total": interrupted_n,
        "longest_gap_between_completed_scans_minutes": round(longest_gap, 6) if longest_gap is not None else None,
        "longest_gap": longest_pair,
        "median_gap_between_completed_scans_minutes": median_gap,
        "expected_interval_minutes": expected,
        "successful_scans_per_utc_day": [
            {"date": day, "completed_scans": scans_by_day[day]} for day in sorted(scans_by_day)
        ],
        "snapshots_per_utc_day": [
            {"date": day, "snapshots": snaps_by_day[day]} for day in sorted(snaps_by_day)
        ],
        "note": (
            "Calendar span is first-to-last stored snapshot. "
            "Accumulated scan execution is the sum of discovery_scans start-to-finish durations "
            "(completed, failed, and interrupted). That is time spent inside scans, not process uptime. "
            "Gaps include power-off, sleep, disabled scheduler, and failed ticks. "
            "Missing minutes are not invented as snapshots. Exact OS uptime is not stored."
        ),
    }
