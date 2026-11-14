"""Automatic discovery scheduler status. Paper intelligence only."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import SchedulerStatusResponse
from app.services.discovery_scheduler import get_scheduler

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatusResponse)
def scheduler_status(db: Session = Depends(get_db)) -> SchedulerStatusResponse:
    return SchedulerStatusResponse.model_validate(get_scheduler().status_payload(db))
