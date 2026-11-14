"""DexScreener discovery scan endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import DiscoveryScan
from app.schemas import DiscoveryScanResponse, DiscoveryStatusResponse, ScanIssueOut
from app.services.dexscreener_client import DexScreenerClient
from app.services.discovery_scheduler import SKIPPED, get_scheduler, skipped_scan_result
from app.services.token_discovery import DiscoveryScanResult, latest_scan

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])


def _scan_response(result: DiscoveryScanResult) -> DiscoveryScanResponse:
    return DiscoveryScanResponse(
        status=result.status,
        profiles_fetched=result.profiles_fetched,
        latest_boosts_fetched=result.latest_boosts_fetched,
        top_boosts_fetched=result.top_boosts_fetched,
        unique_solana_addresses=result.unique_solana_addresses,
        tokens_processed=result.tokens_processed,
        snapshots_saved=result.snapshots_saved,
        stored_tokens_queued=result.stored_tokens_queued,
        below_threshold_count=result.below_threshold_count,
        started_at=result.started_at,
        finished_at=result.finished_at,
        errors=[ScanIssueOut.model_validate(issue.__dict__) for issue in result.errors],
        skipped=[ScanIssueOut.model_validate(issue.__dict__) for issue in result.skipped],
    )


def _scan_from_row(row: DiscoveryScan) -> DiscoveryScanResponse:
    errors = json.loads(row.errors_json or "[]")
    skipped = json.loads(row.skipped_json or "[]")
    return DiscoveryScanResponse(
        status=row.status,
        profiles_fetched=row.profiles_fetched,
        latest_boosts_fetched=row.latest_boosts_fetched,
        top_boosts_fetched=row.top_boosts_fetched,
        unique_solana_addresses=row.unique_solana_addresses,
        tokens_processed=row.tokens_processed,
        snapshots_saved=row.snapshots_saved,
        below_threshold_count=row.below_threshold_count,
        started_at=row.started_at,
        finished_at=row.finished_at,
        errors=[ScanIssueOut.model_validate(item) for item in errors],
        skipped=[ScanIssueOut.model_validate(item) for item in skipped],
    )


@router.post("/scan", response_model=DiscoveryScanResponse)
async def run_scan(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DiscoveryScanResponse | JSONResponse:
    scheduler = get_scheduler()
    if scheduler.is_busy():
        scheduler.record_skip()
        payload = _scan_response(skipped_scan_result())
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
    async with DexScreenerClient(settings=settings) as client:
        result = await scheduler.try_execute(db, settings, client)
    payload = _scan_response(result)
    if result.status == SKIPPED:
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
    if result.fatal:
        return JSONResponse(status_code=502, content=payload.model_dump(mode="json"))
    return payload


@router.get("/status", response_model=DiscoveryStatusResponse)
def discovery_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DiscoveryStatusResponse:
    row = latest_scan(db)
    last_scan = _scan_from_row(row) if row is not None else None
    last_at: datetime | None = None
    if row is not None:
        last_at = row.finished_at or row.started_at
    return DiscoveryStatusResponse(
        integration="dexscreener",
        configured=True,
        live_verified=False,
        base_url=settings.dexscreener_base_url,
        last_scan_at=last_at,
        last_scan_status=row.status if row is not None else "never",
        last_scan=last_scan,
        note=(
            "Adapter is implemented against the public DexScreener API. "
            "live_verified stays false until an operator confirms a live scan. "
            "Appearance on DexScreener is not a token-safety rating. "
            "Automatic collection uses the in-process scheduler when AUTO_DISCOVERY_ENABLED=true "
            f"(interval {settings.auto_discovery_interval_minutes} minutes). "
            "See GET /api/v1/scheduler/status. Manual POST /api/v1/discovery/scan shares the same lock. "
            f"Checked at {datetime.now(timezone.utc).isoformat()}."
        ),
    )
