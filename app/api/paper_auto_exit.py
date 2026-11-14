"""Manual paper auto-exit cycle APIs. Same gates as the optional scheduler."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import PaperAutoExitAttempt, PaperAutoExitCycle
from app.schemas import PaperAutoExitCycleOut, PaperAutoExitStatusOut
from app.services.paper_auto_exit import (
    STATUS_SKIPPED,
    TRIGGER_MANUAL,
    cycle_public,
    monitor_open_positions,
    run_auto_exit_cycle,
)
from app.services.paper_exit_scheduler import get_exit_scheduler

router = APIRouter(prefix="/api/v1/paper/auto-exit", tags=["paper-auto-exit"])


def _cycle_out(summary) -> PaperAutoExitCycleOut:
    return PaperAutoExitCycleOut(
        id=summary.cycle_id,
        trigger=summary.trigger,
        status=summary.status,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        positions_evaluated=summary.positions_evaluated,
        triggered=summary.triggered,
        exited=summary.exited,
        pending=summary.pending,
        skipped=summary.skipped,
        failed=summary.failed,
        orders_created=summary.orders_created,
        attempts=summary.attempts,
        errors=summary.errors,
        market_execution=False,
        wallet=False,
        note=summary.note,
    )


@router.post("/run", response_model=PaperAutoExitCycleOut)
def run_auto_exit(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperAutoExitCycleOut:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    summary = run_auto_exit_cycle(db, settings, trigger=TRIGGER_MANUAL)
    if summary.status == STATUS_SKIPPED and summary.cycle_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another paper-book cycle is running. Fail closed; no exits were submitted.",
        )
    return _cycle_out(summary)


@router.get("/status", response_model=PaperAutoExitStatusOut)
def auto_exit_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperAutoExitStatusOut:
    scheduler = get_exit_scheduler()
    payload = scheduler.status_payload()
    latest = db.scalar(select(PaperAutoExitCycle).order_by(PaperAutoExitCycle.id.desc()).limit(1))
    payload["last_cycle"] = cycle_public(latest) if latest is not None else None
    payload["enabled"] = bool(settings.paper_auto_exit_enabled)
    return PaperAutoExitStatusOut.model_validate(payload)


@router.get("/history")
def auto_exit_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    rows = list(
        db.scalars(select(PaperAutoExitCycle).order_by(PaperAutoExitCycle.id.desc()).limit(limit)).all()
    )
    items = []
    for row in rows:
        payload = cycle_public(row)
        attempts = list(
            db.scalars(
                select(PaperAutoExitAttempt)
                .where(PaperAutoExitAttempt.cycle_id == row.id)
                .order_by(PaperAutoExitAttempt.id.asc())
            ).all()
        )
        payload["attempts"] = [
            {
                "id": item.id,
                "mint_address": item.mint_address,
                "position_id": item.position_id,
                "order_id": item.order_id,
                "outcome": item.outcome,
                "reason": item.reason,
                "quantity": item.quantity,
                "created_at": item.created_at,
            }
            for item in attempts
        ]
        items.append(payload)
    return {
        "items": items,
        "note": (
            "Append-only paper auto-exit cycles. Simulated fills are not market executions. "
            "An unfilled stop is not successful risk protection."
        ),
    }


@router.get("/positions")
def auto_exit_positions(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    monitors = [item.as_dict() for item in monitor_open_positions(db, settings, persist_mark=False)]
    return {
        "items": monitors,
        "note": "Read-only exit monitors from stored evidence. No DexScreener refresh. Not live orders.",
        "market_execution": False,
        "wallet": False,
    }
