"""Opportunity ranking API. Paper intelligence only. No live trading."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import IntelligenceScan, OpportunityAssessment, Token
from app.schemas import (
    OpportunityAssessmentOut,
    OpportunityComponentOut,
    OpportunityFreshnessOut,
    OpportunityHistoryResponse,
    OpportunityListResponse,
    OpportunityScanRequest,
    OpportunityScanResponse,
    OpportunityStatusResponse,
    TokenOpportunityOut,
)
from app.services.opportunity_scan import (
    assessment_is_stale,
    assess_stored_token,
    latest_opportunity,
    load_usable_snapshots,
    scan_opportunities,
)
from app.services.opportunity_scoring import OpportunityResult, classify_opportunity

router = APIRouter(prefix="/api/v1/opportunities", tags=["opportunities"])


@router.post("/scan", response_model=OpportunityScanResponse)
def scan_opportunities_endpoint(
    payload: OpportunityScanRequest = Body(default_factory=OpportunityScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OpportunityScanResponse:
    result = scan_opportunities(
        db,
        settings,
        limit=payload.limit,
        force=payload.force,
    )
    return OpportunityScanResponse(
        status=result.status,
        tokens_considered=result.tokens_considered,
        tokens_scanned=result.tokens_scanned,
        skipped=result.skipped,
        scored=result.scored,
        low_confidence=result.low_confidence,
        insufficient_data=result.insufficient_data,
        high_opportunity=result.high_opportunity,
        watchlist=result.watchlist,
        low_opportunity=result.low_opportunity,
        reliable_classifications=result.reliable_classifications,
        tokens_with_narrative_matches=result.tokens_with_narrative_matches,
        tokens_with_confirmed_security=result.tokens_with_confirmed_security,
        tokens_with_single_snapshot=result.tokens_with_single_snapshot,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


@router.get("/status", response_model=OpportunityStatusResponse)
def opportunity_status(db: Session = Depends(get_db)) -> OpportunityStatusResponse:
    row = db.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == "opportunity")
        .order_by(IntelligenceScan.started_at.desc())
        .limit(1)
    )
    stamp = None
    if row and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return OpportunityStatusResponse(
        last_scan_status=row.status if row else "never",
        last_scan_at=stamp,
        tokens_scanned=row.items_saved if row else None,
        note=(
            "Ranks tokens from stored DexScreener snapshots, narrative matches, and security "
            "assessments. No live RPC or social fetch runs during ranking."
        ),
    )


@router.get("", response_model=OpportunityListResponse)
def list_opportunities(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    assessment_status: str | None = Query(default=None),
    ranking_label: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    min_liquidity_usd: float | None = Query(default=None, ge=0),
    exclude_insufficient: bool = Query(default=False),
    include_stale: bool = Query(default=False),
) -> OpportunityListResponse:
    tokens = list(db.scalars(select(Token)).all())
    ranked: list[TokenOpportunityOut] = []
    for token in tokens:
        payload = _current_token_out(db, token, settings, include_stale=include_stale)
        if payload is None or payload.assessment is None:
            continue
        viewed = payload.assessment
        if payload.freshness and payload.freshness.is_stale and not include_stale:
            continue
        if assessment_status and viewed.status != assessment_status:
            continue
        if ranking_label and viewed.ranking_label != ranking_label:
            continue
        if exclude_insufficient and viewed.status == "INSUFFICIENT_DATA":
            continue
        if min_confidence is not None and viewed.confidence < min_confidence:
            continue
        if min_liquidity_usd is not None and (payload.liquidity_usd is None or payload.liquidity_usd < min_liquidity_usd):
            continue
        ranked.append(payload)
    ranked.sort(
        key=lambda item: (
            not (item.freshness.is_stale if item.freshness else False),
            item.assessment.opportunity_score is not None,
            item.assessment.opportunity_score if item.assessment and item.assessment.opportunity_score is not None else -1,
        ),
        reverse=True,
    )
    return OpportunityListResponse(
        items=ranked[offset : offset + limit],
        total=len(ranked),
        limit=limit,
        offset=offset,
    )


@router.get("/{token_address}", response_model=TokenOpportunityOut)
def get_opportunity(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenOpportunityOut:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    payload = _current_token_out(db, token, settings, include_stale=False)
    if payload is None:
        return TokenOpportunityOut(
            token_address=token.token_address,
            symbol=token.symbol,
            name=token.name,
            assessment=None,
        )
    return payload


@router.get("/{token_address}/history", response_model=OpportunityHistoryResponse)
def get_opportunity_history(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OpportunityHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    rows = list(
        db.scalars(
            select(OpportunityAssessment)
            .where(OpportunityAssessment.token_id == token.id)
            .order_by(OpportunityAssessment.assessed_at.asc())
        ).all()
    )
    return OpportunityHistoryResponse(
        token_address=token.token_address,
        items=[_assessment_out(row, settings) for row in rows],
    )


def _current_token_out(
    db: Session,
    token: Token,
    settings: Settings,
    include_stale: bool = False,
) -> TokenOpportunityOut | None:
    now = datetime.now(timezone.utc)
    persisted = latest_opportunity(db, token.id)
    snapshots = load_usable_snapshots(db, token.id, now)
    current_count = len(snapshots)
    last_snap = snapshots[-1].recorded_at if snapshots else None
    liquidity = snapshots[-1].liquidity_usd if snapshots else None
    stale, reason, _, _ = assessment_is_stale(db, token.id, persisted, now)

    if persisted is None and current_count == 0:
        return None

    source = "persisted"
    viewed: OpportunityAssessmentOut | None = None
    assessed_at = persisted.assessed_at if persisted else None
    assessment_count = persisted.snapshot_count if persisted else None

    if persisted is not None and not stale:
        viewed = _assessment_out(persisted, settings)
        source = "persisted"
        assessment_count = viewed.snapshot_count
    elif stale and include_stale and persisted is not None:
        viewed = _assessment_out(persisted, settings)
        source = "persisted"
        assessment_count = viewed.snapshot_count
    elif stale or persisted is None:
        recomputed = assess_stored_token(db, settings, token)
        viewed = _result_out(recomputed)
        source = "recomputed_from_stored_snapshots"
        assessed_at = recomputed.assessed_at
        assessment_count = recomputed.snapshot_count
        stale = False
        reason = None

    if viewed is None:
        return None

    age = None
    if assessed_at is not None:
        stamp = assessed_at if assessed_at.tzinfo else assessed_at.replace(tzinfo=timezone.utc)
        age = max((now - stamp).total_seconds(), 0)
    return TokenOpportunityOut(
        token_address=token.token_address,
        symbol=token.symbol,
        name=token.name,
        liquidity_usd=liquidity,
        snapshot_count=current_count,
        last_snapshot_at=last_snap,
        assessment=viewed,
        freshness=OpportunityFreshnessOut(
            assessed_at=assessed_at,
            persisted_assessed_at=persisted.assessed_at if persisted else None,
            assessment_snapshot_count=assessment_count,
            current_snapshot_count=current_count,
            assessment_age_seconds=age,
            is_stale=stale,
            stale_reason=reason,
            source=source,
        ),
    )


def _result_out(result: OpportunityResult) -> OpportunityAssessmentOut:
    return OpportunityAssessmentOut(
        status=result.status,
        ranking_label=result.ranking_label,
        opportunity_score=result.opportunity_score,
        positive_opportunity_score=result.positive_opportunity_score,
        security_adjustment=result.security_adjustment,
        confidence=result.confidence,
        data_completeness=result.data_completeness,
        available_components=result.available_components,
        missing_components=result.missing_components,
        positive_signals=result.positive_signals,
        negative_signals=result.negative_signals,
        warnings=result.warnings,
        components=[
            OpportunityComponentOut(
                name=item.name,
                weight=item.weight,
                score=item.score,
                confidence=item.confidence,
                status=item.status,
                explanation=item.explanation,
                metrics=item.metrics,
            )
            for item in result.components
        ],
        explanation=result.explanation,
        assessed_at=result.assessed_at,
        snapshot_count=result.snapshot_count,
        last_snapshot_at=result.last_snapshot_at,
        snapshot_age_hours=result.snapshot_age_hours,
        narrative_match_count=result.narrative_match_count,
        security_coverage=result.security_coverage,
    )


def _assessment_out(row: OpportunityAssessment, settings: Settings) -> OpportunityAssessmentOut:
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=row.positive_opportunity_score,
        opportunity_score=row.opportunity_score,
        data_completeness=row.data_completeness or 0.0,
        confidence=row.confidence or 0.0,
    )
    explanation = row.explanation or ""
    if status != row.status or label != row.ranking_label:
        explanation = (
            f"{status}/{label}: re-evaluated against completeness "
            f">= {settings.opportunity_min_completeness:.2f} and confidence "
            f">= {settings.opportunity_min_confidence:.2f}. "
            f"Stored completeness={row.data_completeness:.2f}, confidence={row.confidence:.2f}. "
            f"Observed scores are retained. Original stored note: {explanation}"
        )
    return OpportunityAssessmentOut(
        status=status,
        ranking_label=label,
        opportunity_score=row.opportunity_score,
        positive_opportunity_score=row.positive_opportunity_score,
        security_adjustment=row.security_adjustment,
        confidence=row.confidence,
        data_completeness=row.data_completeness,
        available_components=_json_list(row.available_components_json),
        missing_components=_json_list(row.missing_components_json),
        positive_signals=_json_list(row.positive_signals_json),
        negative_signals=_json_list(row.negative_signals_json),
        warnings=_json_list(row.warnings_json),
        components=_component_models(row.component_scores_json),
        explanation=explanation,
        assessed_at=row.assessed_at,
        snapshot_count=row.snapshot_count,
        last_snapshot_at=row.last_snapshot_at,
        snapshot_age_hours=row.snapshot_age_hours,
        narrative_match_count=row.narrative_match_count,
        security_coverage=row.security_coverage,
    )


def _json_list(raw: str | None) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload]


def _component_models(raw: str | None) -> list[OpportunityComponentOut]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    items: list[OpportunityComponentOut] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        items.append(
            OpportunityComponentOut(
                name=str(item.get("name") or ""),
                weight=float(item.get("weight") or 0),
                score=item.get("score"),
                confidence=float(item.get("confidence") or 0),
                status=str(item.get("status") or "UNAVAILABLE"),
                explanation=str(item.get("explanation") or ""),
                metrics=item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
            )
        )
    return items
