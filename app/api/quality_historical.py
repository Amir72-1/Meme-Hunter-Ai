"""Quality-aware historical evaluation endpoints. Does not replace original /historical routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import QualityHistoricalEvaluation
from app.schemas import (
    QualityHistoricalEvaluateRequest,
    QualityHistoricalEvaluationListResponse,
    QualityHistoricalEvaluationOut,
    QualityHistoricalStatusResponse,
)
from app.services.historical_evaluation import SUPPORTED_HORIZONS
from app.services.quality_historical_evaluation import (
    get_quality_evaluation,
    latest_quality_evaluation,
    list_quality_evaluations,
    run_quality_historical_evaluation,
)

router = APIRouter(prefix="/api/v1/historical", tags=["historical-quality"])


def _row_out(row: QualityHistoricalEvaluation, *, include_outcomes: bool) -> QualityHistoricalEvaluationOut:
    payload = json.loads(row.summary_json or "{}")
    payload.setdefault("evaluation_id", row.id)
    payload.setdefault("status", row.status)
    payload.setdefault("horizon", row.horizon)
    payload.setdefault("fee_bps", row.fee_bps)
    payload.setdefault("slippage_bps", row.slippage_bps)
    payload.setdefault("round_trip_cost", 2.0 * ((row.fee_bps + row.slippage_bps) / 10_000.0))
    payload.setdefault("tokens_considered", row.tokens_considered)
    payload.setdefault("signals_considered", row.signals_considered)
    payload.setdefault("outcomes_measured", row.outcomes_measured)
    payload.setdefault("snapshots_scanned", row.snapshots_scanned)
    payload.setdefault("coverage", {})
    payload.setdefault("cohorts", {})
    payload.setdefault("overlapping_windows", {})
    payload.setdefault("thresholds", {})
    payload.setdefault("explanation", row.explanation)
    payload.setdefault("paper_trading_only", True)
    if not include_outcomes:
        payload.pop("outcomes", None)
    return QualityHistoricalEvaluationOut.model_validate(payload)


@router.post("/quality-evaluate", response_model=QualityHistoricalEvaluationOut)
def quality_evaluate(
    payload: QualityHistoricalEvaluateRequest = Body(default_factory=QualityHistoricalEvaluateRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    include_outcomes: bool = Query(default=False),
) -> QualityHistoricalEvaluationOut:
    if payload.horizon not in SUPPORTED_HORIZONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported horizon. Use one of: {', '.join(SUPPORTED_HORIZONS)}",
        )
    result = run_quality_historical_evaluation(
        db,
        settings,
        horizon=payload.horizon,
        fee_bps=payload.fee_bps,
        slippage_bps=payload.slippage_bps,
        token_address=payload.token_address,
    )
    if not include_outcomes:
        result = dict(result)
        result.pop("outcomes", None)
    return QualityHistoricalEvaluationOut.model_validate(result)


@router.get("/quality-status", response_model=QualityHistoricalStatusResponse)
def quality_historical_status(db: Session = Depends(get_db)) -> QualityHistoricalStatusResponse:
    row = latest_quality_evaluation(db)
    return QualityHistoricalStatusResponse(
        last_status=row.status if row else "never",
        last_horizon=row.horizon if row else None,
        last_evaluation_at=(row.finished_at or row.started_at).isoformat() if row else None,
        last_sample_size=row.outcomes_measured if row else None,
        last_evaluation_id=row.id if row else None,
        note=(
            "Quality-aware evaluation of stored snapshots. Separate from evaluations 1 and 2. "
            "Flags use information at or before each outcome time. Not executable returns. Paper trading only."
        ),
    )


@router.get("/quality-evaluations", response_model=QualityHistoricalEvaluationListResponse)
def list_quality_historical_evaluations(
    db: Session = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> QualityHistoricalEvaluationListResponse:
    rows, total = list_quality_evaluations(db, limit=limit, offset=offset)
    return QualityHistoricalEvaluationListResponse(
        items=[_row_out(row, include_outcomes=False) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/quality-evaluations/{evaluation_id}", response_model=QualityHistoricalEvaluationOut)
def get_quality_historical_evaluation(
    evaluation_id: int,
    db: Session = Depends(get_db),
    include_outcomes: bool = Query(default=False),
) -> QualityHistoricalEvaluationOut:
    row = get_quality_evaluation(db, evaluation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quality evaluation not found")
    return _row_out(row, include_outcomes=include_outcomes)
