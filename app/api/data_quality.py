"""Stored-snapshot data-quality endpoints. No live APIs or trading."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Token
from app.schemas import (
    DataQualityScanRequest,
    DataQualityScanResponse,
    DataQualityStatusResponse,
    DataQualityTokenOut,
)
from app.services.market_data_quality import (
    latest_finding_for_token,
    latest_quality_scan,
    run_quality_scan,
)

router = APIRouter(prefix="/api/v1/data-quality", tags=["data-quality"])


def _scan_out(payload: dict, include_items: bool) -> DataQualityScanResponse:
    if not include_items:
        payload = dict(payload)
        payload.pop("items", None)
    return DataQualityScanResponse.model_validate(payload)


@router.post("/scan", response_model=DataQualityScanResponse)
def scan_data_quality(
    payload: DataQualityScanRequest = Body(default_factory=DataQualityScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    include_items: bool = Query(default=True),
) -> DataQualityScanResponse:
    result = run_quality_scan(db, settings, token_address=payload.token_address)
    return _scan_out(result, include_items=include_items)


@router.get("/status", response_model=DataQualityStatusResponse)
def data_quality_status(db: Session = Depends(get_db)) -> DataQualityStatusResponse:
    row = latest_quality_scan(db)
    classifications = None
    if row is not None:
        classifications = {
            "CLEAN": row.clean_count,
            "WARNING": row.warning_count,
            "SUSPICIOUS": row.suspicious_count,
            "INSUFFICIENT_DATA": row.insufficient_data_count,
        }
    return DataQualityStatusResponse(
        last_status=row.status if row else "never",
        last_scan_at=(row.finished_at or row.started_at).isoformat() if row else None,
        last_tokens_scanned=row.tokens_scanned if row else None,
        last_classifications=classifications,
        note=(
            "Analyzes stored DexScreener snapshots only. CLEAN is not a buy signal. "
            "Suspicious flags investigate quote quality, not token safety. Paper trading only."
        ),
    )


@router.get("/summary", response_model=DataQualityScanResponse)
def data_quality_summary(db: Session = Depends(get_db)) -> DataQualityScanResponse:
    row = latest_quality_scan(db)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No data-quality scan has been run")
    payload = json.loads(row.summary_json or "{}")
    payload.setdefault("scan_id", row.id)
    payload.setdefault("status", row.status)
    payload.setdefault("tokens_scanned", row.tokens_scanned)
    payload.setdefault("snapshots_scanned", row.snapshots_scanned)
    payload.setdefault(
        "classifications",
        {
            "CLEAN": row.clean_count,
            "WARNING": row.warning_count,
            "SUSPICIOUS": row.suspicious_count,
            "INSUFFICIENT_DATA": row.insufficient_data_count,
        },
    )
    payload.setdefault(
        "liquidity",
        {
            "total_observations": row.snapshots_scanned,
            "non_null_liquidity": row.liquidity_non_null,
            "missing_liquidity": row.liquidity_missing,
            "missing_pct": (
                round(row.liquidity_missing / row.snapshots_scanned, 6) if row.snapshots_scanned else None
            ),
            "note": "Missing liquidity is not treated as zero.",
        },
    )
    payload.setdefault("thresholds", {})
    payload.setdefault("explanation", row.explanation)
    payload.pop("items", None)
    return DataQualityScanResponse.model_validate(payload)


@router.get("/tokens/{token_address}", response_model=DataQualityTokenOut)
def data_quality_token(token_address: str, db: Session = Depends(get_db)) -> DataQualityTokenOut:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    finding = latest_finding_for_token(db, token.id)
    if finding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No data-quality finding for this token")
    flags = json.loads(finding.flags_json or "[]")
    total = finding.snapshot_count
    missing = finding.liquidity_missing
    return DataQualityTokenOut(
        token_address=token.token_address,
        symbol=token.symbol,
        classification=finding.classification,
        flags=flags,
        snapshot_count=finding.snapshot_count,
        liquidity={
            "total_observations": total,
            "non_null_liquidity": finding.liquidity_non_null,
            "missing_liquidity": missing,
            "missing_pct": round(missing / total, 6) if total else None,
            "note": "Missing liquidity is not treated as zero.",
        },
        explanation=finding.explanation,
    )
