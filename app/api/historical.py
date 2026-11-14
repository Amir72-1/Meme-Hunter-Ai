"""Market-only historical evaluation endpoints. Stored snapshots only. No live trading."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import HistoricalEvaluation
from app.schemas import (
    HistoricalEvaluateRequest,
    HistoricalEvaluationListResponse,
    HistoricalEvaluationOut,
    HistoricalStatusResponse,
)
from app.services.historical_evaluation import (
    SUPPORTED_HORIZONS,
    get_evaluation,
    latest_evaluation,
    list_evaluations,
    run_historical_evaluation,
)

router = APIRouter(prefix="/api/v1/historical", tags=["historical"])


def _row_out(row: HistoricalEvaluation) -> HistoricalEvaluationOut:
    payload = json.loads(row.summary_json or "{}")
    payload.setdefault("evaluation_id", row.id)
    payload.setdefault("status", row.status)
    payload.setdefault("horizon", row.horizon)
    payload.setdefault("fee_bps", row.fee_bps)
    payload.setdefault("slippage_bps", row.slippage_bps)
    payload.setdefault("round_trip_cost", 2.0 * ((row.fee_bps + row.slippage_bps) / 10_000.0))
    payload.setdefault("sample_size", row.outcomes_measured)
    payload.setdefault("explanation", row.explanation)
    payload.setdefault(
        "coverage",
        {
            "tokens_considered": row.tokens_considered,
            "signals_considered": row.signals_considered,
            "outcomes_measured": row.outcomes_measured,
            "outcome_coverage": None,
            "excluded_gap": row.excluded_gap,
            "excluded_missing_valuation": row.excluded_missing_valuation,
            "excluded_missing_outcome": row.excluded_missing_outcome,
            "excluded_insufficient_history": row.excluded_insufficient_history,
            "valuation_changes_measured": 0,
            "volume_changes_measured": 0,
            "liquidity_changes_measured": 0,
            "liquidity_missing_at_signal": 0,
            "liquidity_missing_rate_at_signal": None,
            "mfe_mae_measured": 0,
        },
    )
    payload.setdefault("gross_return", {"avg": None, "median": None, "min": None, "max": None, "count": 0})
    payload.setdefault("net_return", {"avg": None, "median": None, "min": None, "max": None, "count": 0})
    payload.setdefault(
        "max_favorable_excursion", {"avg": None, "median": None, "min": None, "max": None, "count": 0}
    )
    payload.setdefault(
        "max_adverse_excursion", {"avg": None, "median": None, "min": None, "max": None, "count": 0}
    )
    payload.setdefault("by_category", {})
    payload.setdefault("positive_outcomes", 0)
    payload.setdefault("negative_outcomes", 0)
    payload.setdefault("zero_outcomes", 0)
    payload.setdefault("claim_ready", False)
    payload.setdefault("paper_trading_only", True)
    if payload.get("started_at") is None and row.started_at is not None:
        payload["started_at"] = row.started_at.isoformat()
    if payload.get("finished_at") is None and row.finished_at is not None:
        payload["finished_at"] = row.finished_at.isoformat()
    return HistoricalEvaluationOut.model_validate(payload)


@router.post("/evaluate", response_model=HistoricalEvaluationOut)
def evaluate_historical(
    payload: HistoricalEvaluateRequest = Body(default_factory=HistoricalEvaluateRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HistoricalEvaluationOut:
    if payload.horizon not in SUPPORTED_HORIZONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported horizon. Use one of: {', '.join(SUPPORTED_HORIZONS)}",
        )
    result = run_historical_evaluation(
        db,
        settings,
        horizon=payload.horizon,
        fee_bps=payload.fee_bps,
        slippage_bps=payload.slippage_bps,
        token_address=payload.token_address,
    )
    return HistoricalEvaluationOut.model_validate(result.as_dict())


@router.get("/status", response_model=HistoricalStatusResponse)
def historical_status(db: Session = Depends(get_db)) -> HistoricalStatusResponse:
    row = latest_evaluation(db)
    return HistoricalStatusResponse(
        last_status=row.status if row else "never",
        last_horizon=row.horizon if row else None,
        last_evaluation_at=(row.finished_at or row.started_at).isoformat() if row else None,
        last_sample_size=row.outcomes_measured if row else None,
        note=(
            "Evaluates stored DexScreener snapshots only. Horizons: next_observation (~15 minutes) "
            "and one_hour. Gaps over 30 minutes are missing intervals. Hypothetical fees/slippage "
            "are not fills. Paper trading only; no wallet or live orders."
        ),
    )


@router.get("/evaluations", response_model=HistoricalEvaluationListResponse)
def list_historical_evaluations(
    db: Session = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> HistoricalEvaluationListResponse:
    rows, total = list_evaluations(db, limit=limit, offset=offset)
    return HistoricalEvaluationListResponse(
        items=[_row_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/evaluations/{evaluation_id}", response_model=HistoricalEvaluationOut)
def get_historical_evaluation(evaluation_id: int, db: Session = Depends(get_db)) -> HistoricalEvaluationOut:
    row = get_evaluation(db, evaluation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found")
    return _row_out(row)
