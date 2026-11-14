"""Read-only data-readiness and collection-health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.schemas import CollectionContinuityOut, CollectionHealthOut, ReadinessReportOut
from app.services.data_readiness import build_readiness_report
from app.services.discovery_scheduler import get_scheduler

router = APIRouter(prefix="/api/v1/readiness", tags=["readiness"])


@router.get("", response_model=ReadinessReportOut)
def data_readiness(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReadinessReportOut:
    payload = build_readiness_report(db, settings, get_scheduler())
    return ReadinessReportOut.model_validate(payload)


@router.get("/health", response_model=CollectionHealthOut)
def collection_health_endpoint(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CollectionHealthOut:
    payload = build_readiness_report(db, settings, get_scheduler())
    return CollectionHealthOut.model_validate(payload["collection_health"])


@router.get("/continuity", response_model=CollectionContinuityOut)
def collection_continuity_endpoint(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CollectionContinuityOut:
    payload = build_readiness_report(db, settings, get_scheduler())
    return CollectionContinuityOut.model_validate(payload["collection_continuity"])
