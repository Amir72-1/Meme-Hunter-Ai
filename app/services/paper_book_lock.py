"""Shared SQLite lease for paper book mutations (auto-entry and auto-exit)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import PaperAutoEntryCycle, PaperAutoEntryLock, PaperAutoExitCycle
from app.timeutil import as_utc

LOCK_ID = 1
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"


def book_lease_seconds(settings: Settings) -> float:
    return max(
        float(settings.paper_auto_entry_lease_seconds),
        float(settings.paper_auto_exit_lease_seconds),
    )


def ensure_lock_row(session: Session) -> None:
    row = session.get(PaperAutoEntryLock, LOCK_ID)
    if row is None:
        session.add(PaperAutoEntryLock(id=LOCK_ID))
        session.commit()


def interrupt_stale_book_cycles(session: Session, now: datetime) -> None:
    stale_before = now - timedelta(seconds=1)
    lock = session.get(PaperAutoEntryLock, LOCK_ID)
    until = as_utc(lock.lease_until) if lock is not None else None
    live = bool(lock is not None and lock.holder and until is not None and until > now)
    kind = getattr(lock, "cycle_kind", None) if lock is not None else None
    owned_id = lock.cycle_id if lock is not None else None

    entry_rows = list(
        session.scalars(select(PaperAutoEntryCycle).where(PaperAutoEntryCycle.status == STATUS_RUNNING)).all()
    )
    for cycle in entry_rows:
        started = as_utc(cycle.started_at)
        if started is None or started > stale_before:
            continue
        if live and kind == "entry" and owned_id == cycle.id:
            continue
        cycle.status = STATUS_INTERRUPTED
        cycle.finished_at = now
        errors = json.loads(cycle.errors_json) if cycle.errors_json else []
        errors.append({"detail": "Lease expired. Fail closed; no further paper-book mutations from this cycle."})
        cycle.errors_json = json.dumps(errors)

    exit_rows = list(
        session.scalars(select(PaperAutoExitCycle).where(PaperAutoExitCycle.status == STATUS_RUNNING)).all()
    )
    for cycle in exit_rows:
        started = as_utc(cycle.started_at)
        if started is None or started > stale_before:
            continue
        if live and kind == "exit" and owned_id == cycle.id:
            continue
        cycle.status = STATUS_INTERRUPTED
        cycle.finished_at = now
        errors = json.loads(cycle.errors_json) if cycle.errors_json else []
        errors.append({"detail": "Lease expired. Fail closed; no further paper-book mutations from this cycle."})
        cycle.errors_json = json.dumps(errors)
    session.commit()


def acquire_book_lock(
    session: Session,
    settings: Settings,
    holder: str,
    now: datetime,
) -> bool:
    if not settings.sqlite_backend:
        return False
    ensure_lock_row(session)
    interrupt_stale_book_cycles(session, now)
    stamp = as_utc(now).replace(tzinfo=None)
    until = (as_utc(now) + timedelta(seconds=book_lease_seconds(settings))).replace(tzinfo=None)
    result = session.execute(
        text(
            "UPDATE paper_auto_entry_locks "
            "SET holder = :holder, acquired_at = :stamp, lease_until = :until, cycle_id = NULL, cycle_kind = NULL "
            "WHERE id = 1 AND (holder IS NULL OR lease_until IS NULL OR lease_until < :stamp)"
        ),
        {"holder": holder, "stamp": stamp, "until": until},
    )
    session.commit()
    session.expire_all()
    return int(result.rowcount or 0) == 1


def bind_book_cycle(session: Session, cycle_id: int, kind: str) -> None:
    lock = session.get(PaperAutoEntryLock, LOCK_ID)
    if lock is None:
        return
    lock.cycle_id = cycle_id
    lock.cycle_kind = kind
    session.commit()


def release_book_lock(session: Session, holder: str) -> None:
    lock = session.get(PaperAutoEntryLock, LOCK_ID)
    if lock is None or lock.holder != holder:
        return
    lock.holder = None
    lock.cycle_id = None
    lock.cycle_kind = None
    lock.acquired_at = None
    lock.lease_until = None
    session.commit()
    session.expire_all()
