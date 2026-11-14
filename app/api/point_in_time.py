"""Point-in-time validation endpoints. Does not replace historical or quality routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import PointInTimeRun
from app.schemas import (
    PointInTimeEvaluateRequest,
    PointInTimeRunListResponse,
    PointInTimeRunOut,
    PointInTimeStatusResponse,
)
from app.services.point_in_time_validation import (
    GROUPS,
    OVERLAP_POLICIES,
    SPLIT_HOLDOUT,
    SPLIT_PURGE,
    SPLIT_RESEARCH,
    filter_summary,
    get_point_in_time_run,
    latest_point_in_time_run,
    list_point_in_time_runs,
    run_point_in_time_evaluation,
)

router = APIRouter(prefix="/api/v1/historical", tags=["point-in-time"])


def _row_out(
    row: PointInTimeRun,
    *,
    split: str | None = None,
    group: str | None = None,
    overlap: str | None = None,
) -> PointInTimeRunOut:
    payload = json.loads(row.summary_json or "{}")
    payload.setdefault("run_id", row.id)
    payload.setdefault("status", row.status)
    payload.setdefault("snapshots_scanned", row.snapshots_scanned)
    payload.setdefault("candidate_count", row.candidates_count)
    payload.setdefault("groups_before_overlap", {})
    payload.setdefault("g1_missing_liquidity", {})
    payload.setdefault("g3_count", 0)
    payload.setdefault("g3_insufficient", True)
    payload.setdefault("split", {})
    payload.setdefault("metrics", {})
    payload.setdefault("thresholds", {})
    payload.setdefault("explanation", row.explanation)
    payload.setdefault("paper_trading_only", True)
    payload = filter_summary(payload, split=split, group=group, overlap=overlap)
    return PointInTimeRunOut.model_validate(payload)


@router.post("/point-in-time-evaluate", response_model=PointInTimeRunOut)
def point_in_time_evaluate(
    payload: PointInTimeEvaluateRequest = Body(default_factory=PointInTimeEvaluateRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PointInTimeRunOut:
    result = run_point_in_time_evaluation(db, settings, token_address=payload.token_address)
    return PointInTimeRunOut.model_validate(result)


@router.get("/point-in-time-status", response_model=PointInTimeStatusResponse)
def point_in_time_status(db: Session = Depends(get_db)) -> PointInTimeStatusResponse:
    row = latest_point_in_time_run(db)
    g3 = None
    if row and row.summary_json:
        g3 = json.loads(row.summary_json).get("g3_count")
    return PointInTimeStatusResponse(
        last_status=row.status if row else "never",
        last_run_at=(row.finished_at or row.started_at).isoformat() if row else None,
        last_run_id=row.id if row else None,
        last_candidate_count=row.candidates_count if row else None,
        last_g3_count=g3,
        note=(
            "Point-in-time validation of stored snapshots. Not paper trading. "
            "G3 may be zero. Missing liquidity stays null. Evaluations 1–2 are unchanged."
        ),
    )


@router.get("/point-in-time-runs", response_model=PointInTimeRunListResponse)
def list_point_in_time(
    db: Session = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PointInTimeRunListResponse:
    rows, total = list_point_in_time_runs(db, limit=limit, offset=offset)
    return PointInTimeRunListResponse(
        items=[_row_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/point-in-time-runs/{run_id}", response_model=PointInTimeRunOut)
def get_point_in_time(
    run_id: int,
    db: Session = Depends(get_db),
    split: str | None = Query(default=None),
    group: str | None = Query(default=None),
    overlap: str | None = Query(default=None),
) -> PointInTimeRunOut:
    if split is not None and split not in {SPLIT_RESEARCH, SPLIT_PURGE, SPLIT_HOLDOUT, "all"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid split")
    if group is not None and group not in GROUPS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid group")
    if overlap is not None and overlap not in OVERLAP_POLICIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid overlap")
    row = get_point_in_time_run(db, run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Point-in-time run not found")
    return _row_out(row, split=None if split == "all" else split, group=group, overlap=overlap)
