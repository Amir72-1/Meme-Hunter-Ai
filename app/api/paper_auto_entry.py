"""Manual paper auto-entry cycle APIs. Same gates as the optional scheduler."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import PaperAutoEntryAttempt, PaperAutoEntryCycle
from app.schemas import PaperAutoEntryCycleOut, PaperAutoEntryRunRequest, PaperAutoEntryStatusOut
from app.services.paper_auto_entry import (
    STATUS_SKIPPED,
    TRIGGER_MANUAL,
    cycle_public,
    run_auto_entry_cycle,
)
from app.services.paper_entry_scheduler import get_entry_scheduler

router = APIRouter(prefix="/api/v1/paper/auto-entry", tags=["paper-auto-entry"])


def _cycle_out(summary) -> PaperAutoEntryCycleOut:
    return PaperAutoEntryCycleOut(
        id=summary.cycle_id,
        trigger=summary.trigger,
        status=summary.status,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        tokens_evaluated=summary.tokens_evaluated,
        eligible=summary.eligible,
        entered=summary.entered,
        rejected=summary.rejected,
        waiting=summary.waiting,
        failed=summary.failed,
        skipped=summary.skipped,
        orders_created=summary.orders_created,
        attempts=summary.attempts,
        errors=summary.errors,
        automatic_selling=False,
        market_execution=False,
        note=summary.note,
    )


@router.post("/run", response_model=PaperAutoEntryCycleOut)
def run_auto_entry(
    payload: PaperAutoEntryRunRequest = Body(default_factory=PaperAutoEntryRunRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperAutoEntryCycleOut:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    summary = run_auto_entry_cycle(
        db,
        settings,
        trigger=TRIGGER_MANUAL,
        token_addresses=payload.token_addresses,
    )
    if summary.status == STATUS_SKIPPED and summary.cycle_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another auto-entry cycle is running. Fail closed; no orders were submitted.",
        )
    return _cycle_out(summary)


@router.get("/status", response_model=PaperAutoEntryStatusOut)
def auto_entry_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperAutoEntryStatusOut:
    scheduler = get_entry_scheduler()
    payload = scheduler.status_payload()
    latest = db.scalar(select(PaperAutoEntryCycle).order_by(PaperAutoEntryCycle.id.desc()).limit(1))
    payload["last_cycle"] = cycle_public(latest) if latest is not None else None
    payload["enabled"] = bool(settings.paper_auto_entry_enabled)
    return PaperAutoEntryStatusOut.model_validate(payload)


@router.get("/history")
def auto_entry_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    rows = list(
        db.scalars(
            select(PaperAutoEntryCycle).order_by(PaperAutoEntryCycle.id.desc()).limit(limit)
        ).all()
    )
    items = []
    for row in rows:
        payload = cycle_public(row)
        attempts = list(
            db.scalars(
                select(PaperAutoEntryAttempt)
                .where(PaperAutoEntryAttempt.cycle_id == row.id)
                .order_by(PaperAutoEntryAttempt.id.asc())
            ).all()
        )
        payload["attempts"] = [
            {
                "id": item.id,
                "mint_address": item.mint_address,
                "decision_id": item.decision_id,
                "order_id": item.order_id,
                "outcome": item.outcome,
                "reason": item.reason,
                "notional_usd": item.notional_usd,
                "created_at": item.created_at,
            }
            for item in attempts
        ]
        items.append(payload)
    return {
        "items": items,
        "note": "Append-only paper auto-entry cycles. Simulated fills are not market executions.",
    }
