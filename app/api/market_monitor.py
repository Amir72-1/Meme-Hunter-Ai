"""Null-aware market condition monitor. Stored snapshots only; no discovery lock."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Token
from app.schemas import (
    MarketConditionFindingOut,
    MarketMonitorScanRequest,
    MarketMonitorScanResponse,
    MarketMonitorStatusResponse,
    MarketMonitorTokenOut,
)
from app.services.market_condition_monitor import (
    latest_monitor_scan,
    report_for_token,
    run_market_monitor_scan,
)

router = APIRouter(prefix="/api/v1/market-monitor", tags=["market-monitor"])


@router.post("/scan", response_model=MarketMonitorScanResponse)
def scan_market_conditions(
    payload: MarketMonitorScanRequest = Body(default_factory=MarketMonitorScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    include_items: bool = Query(default=True),
) -> MarketMonitorScanResponse:
    result = run_market_monitor_scan(
        db,
        settings,
        token_address=payload.token_address,
        limit=payload.limit,
    )
    if not include_items:
        result = dict(result)
        result.pop("items", None)
    return _scan_out(result)


@router.get("/status", response_model=MarketMonitorStatusResponse)
def market_monitor_status(db: Session = Depends(get_db)) -> MarketMonitorStatusResponse:
    row = latest_monitor_scan(db)
    details = json.loads(row.details_json or "{}") if row is not None else {}
    stamp = None
    if row is not None and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return MarketMonitorStatusResponse(
        last_status=row.status if row else "never",
        last_scan_at=stamp,
        last_tokens_scanned=row.items_saved if row else None,
        last_liquidity_decreases=details.get("liquidity_decreases"),
        note=(
            "Read-only monitor of stored DexScreener snapshots. "
            "Does not lock the discovery scheduler. Missing liquidity is not zero. "
            "Paper trading only."
        ),
    )


@router.get("/summary", response_model=MarketMonitorScanResponse)
def market_monitor_summary(db: Session = Depends(get_db)) -> MarketMonitorScanResponse:
    row = latest_monitor_scan(db)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No market-monitor scan has been run")
    payload = json.loads(row.details_json or "{}")
    payload.setdefault("scan_id", row.id)
    payload.setdefault("status", row.status)
    payload.setdefault("tokens_scanned", row.items_saved)
    payload.pop("items", None)
    return _scan_out(payload)


@router.get("/tokens/{token_address}", response_model=MarketMonitorTokenOut)
def market_monitor_token(
    token_address: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MarketMonitorTokenOut:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    report = report_for_token(db, token, as_of, settings)
    return MarketMonitorTokenOut(
        token_address=report.token_address,
        symbol=report.symbol,
        signal_time=report.signal_time,
        snapshot_count=report.snapshot_count,
        availability=report.availability,
        findings=[MarketConditionFindingOut.model_validate(item.as_dict()) for item in report.findings],
        note=report.note,
    )


def _scan_out(payload: dict) -> MarketMonitorScanResponse:
    items = payload.get("items")
    parsed_items = None
    if isinstance(items, list):
        parsed_items = []
        for item in items:
            findings = [MarketConditionFindingOut.model_validate(row) for row in item.get("findings") or []]
            parsed_items.append(
                MarketMonitorTokenOut(
                    token_address=item["token_address"],
                    symbol=item.get("symbol"),
                    signal_time=item["signal_time"],
                    snapshot_count=item.get("snapshot_count") or 0,
                    availability=item.get("availability") or {},
                    findings=findings,
                    note=item.get("note") or "",
                )
            )
    return MarketMonitorScanResponse(
        scan_id=payload.get("scan_id"),
        status=payload.get("status") or "unknown",
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        signal_time=payload.get("signal_time"),
        tokens_scanned=payload.get("tokens_scanned") or 0,
        requested_limit=payload.get("requested_limit"),
        applied_limit=payload.get("applied_limit"),
        max_scan_limit=payload.get("max_scan_limit"),
        liquidity_decreases=payload.get("liquidity_decreases") or 0,
        stale_tokens=payload.get("stale_tokens") or 0,
        thresholds=payload.get("thresholds") or {},
        items=parsed_items,
        note=payload.get("note") or "",
    )
