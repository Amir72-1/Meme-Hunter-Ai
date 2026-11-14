"""Token risk and security API."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import (
    HolderAnalysis,
    IntelligenceScan,
    LiquidityAnalysisRecord,
    RiskScoreHistory,
    Token,
    Token2022ExtensionObservation,
    TokenRiskAssessment,
    TokenSecurityScan,
    WalletActivity,
)
from app.schemas import (
    ControlFindingOut,
    ControlFindingsResponse,
    HighRiskListResponse,
    RiskAssessmentOut,
    RiskCategoryOut,
    SecurityScanRequest,
    SecurityScanResponse,
    SecurityStatusResponse,
    SecuritySummaryResponse,
    TokenSecurityHistoryResponse,
    TokenSecurityOut,
)
from app.services.control_risk_findings import findings_for_token
from app.services.risk_scoring import ClassificationView, classify_measurements
from app.services.security_scan import latest_assessment, scan_tokens
from app.services.solana_rpc import SolanaRpcClient

router = APIRouter(prefix="/api/v1/security", tags=["security"])


@router.post("/scan", response_model=SecurityScanResponse)
async def scan_discovered(
    payload: SecurityScanRequest = Body(default_factory=SecurityScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SecurityScanResponse:
    request = payload
    async with SolanaRpcClient(settings=settings) as rpc:
        result = await scan_tokens(
            db,
            rpc,
            settings,
            limit=request.limit,
            force=request.force,
        )
    return _summary_out(result)


@router.post("/scan/{token_address}", response_model=SecurityScanResponse)
async def scan_one_token(
    token_address: str,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SecurityScanResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    async with SolanaRpcClient(settings=settings) as rpc:
        result = await scan_tokens(
            db,
            rpc,
            settings,
            token_address=token_address,
            force=force,
        )
    return _summary_out(result)


@router.get("/status", response_model=SecurityStatusResponse)
def security_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SecurityStatusResponse:
    row = db.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == "security")
        .order_by(IntelligenceScan.started_at.desc())
        .limit(1)
    )
    stamp = None
    if row and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return SecurityStatusResponse(
        solana_rpc_url=settings.solana_rpc_url,
        solana_rpc_configured=bool(settings.solana_rpc_url),
        last_scan_status=row.status if row else "never",
        last_scan_at=stamp,
        tokens_scanned=row.items_saved if row else None,
        note=(
            "Uses public Solana RPC plus stored DexScreener snapshots. "
            "Metaplex metadata is a current RPC observation, not historical reconstruction. "
            "Liquidity locks are UNKNOWN unless independently verified. "
            "Paper trading only."
        ),
    )


@router.get("/tokens/{token_address}", response_model=TokenSecurityOut)
def get_token_security(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenSecurityOut:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    assessment = latest_assessment(db, token.id)
    if assessment is None:
        return TokenSecurityOut(
            token_address=token.token_address,
            symbol=token.symbol,
            name=token.name,
            assessment=None,
            control_findings=[_finding_out(item) for item in findings_for_token(db, token).findings],
        )
    return _token_out(db, token, assessment, settings)


@router.get("/tokens/{token_address}/findings", response_model=ControlFindingsResponse)
def get_token_control_findings(
    token_address: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ControlFindingsResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    report = findings_for_token(db, token, as_of)
    return ControlFindingsResponse(
        token_address=report.token_address,
        signal_time=report.signal_time,
        findings=[_finding_out(item) for item in report.findings],
        note=report.note,
    )


@router.get("/tokens/{token_address}/history", response_model=TokenSecurityHistoryResponse)
def get_token_security_history(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenSecurityHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    rows = list(
        db.scalars(
            select(RiskScoreHistory)
            .where(RiskScoreHistory.token_id == token.id)
            .order_by(RiskScoreHistory.scanned_at.asc())
        ).all()
    )
    return TokenSecurityHistoryResponse(
        token_address=token.token_address,
        items=[_history_out(row, settings) for row in rows],
    )


@router.get("/high-risk", response_model=HighRiskListResponse)
def high_risk_tokens(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> HighRiskListResponse:
    tokens = list(db.scalars(select(Token)).all())
    confirmed: list[TokenSecurityOut] = []
    provisional: list[TokenSecurityOut] = []
    for token in tokens:
        assessment = latest_assessment(db, token.id)
        if assessment is None:
            continue
        classified = _classify_row(assessment, settings)
        if classified.observed_risk is None or classified.observed_risk < settings.security_high_risk_threshold:
            continue
        payload = _token_out(db, token, assessment, settings)
        if classified.classification_reliable and classified.risk_score is not None:
            confirmed.append(payload)
        else:
            provisional.append(payload)
    confirmed.sort(
        key=lambda item: item.assessment.risk_score if item.assessment and item.assessment.risk_score else 0,
        reverse=True,
    )
    provisional.sort(
        key=lambda item: item.assessment.observed_risk if item.assessment and item.assessment.observed_risk else 0,
        reverse=True,
    )
    return HighRiskListResponse(
        items=confirmed[offset : offset + limit],
        provisional=provisional[offset : offset + limit],
        total=len(confirmed),
        provisional_total=len(provisional),
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=SecuritySummaryResponse)
def security_summary(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> SecuritySummaryResponse:
    tokens = list(db.scalars(select(Token)).all())
    assessed = insufficient = low_confidence = high = lower = moderate = 0
    last_at = None
    for token in tokens:
        assessment = latest_assessment(db, token.id)
        if assessment is None:
            continue
        assessed += 1
        if last_at is None or assessment.scanned_at > last_at:
            last_at = assessment.scanned_at
        classified = _classify_row(assessment, settings)
        if classified.status == "INSUFFICIENT_DATA":
            insufficient += 1
        elif classified.status == "LOW_CONFIDENCE":
            low_confidence += 1
        elif (
            classified.classification_reliable
            and classified.risk_score is not None
            and classified.risk_score >= settings.security_high_risk_threshold
        ):
            high += 1
        elif classified.risk_band in {"lower", "moderate_low"}:
            lower += 1
        else:
            moderate += 1
    return SecuritySummaryResponse(
        tokens_assessed=assessed,
        insufficient_data=insufficient,
        low_confidence=low_confidence,
        high_or_critical=high,
        lower=lower,
        moderate=moderate,
        last_scan_at=last_at.isoformat() if last_at else None,
        note=(
            "high_or_critical counts confirmed scored assessments only. "
            "LOW_CONFIDENCE and INSUFFICIENT_DATA are listed separately. Not a safety rating."
        ),
    )


def _summary_out(result) -> SecurityScanResponse:
    return SecurityScanResponse(
        status=result.status,
        tokens_considered=result.tokens_considered,
        tokens_scanned=result.tokens_scanned,
        skipped=result.skipped,
        insufficient_data=result.insufficient_data,
        high_risk=result.high_risk,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


def _token_out(
    db: Session, token: Token, assessment: TokenRiskAssessment, settings: Settings
) -> TokenSecurityOut:
    security = db.scalar(
        select(TokenSecurityScan)
        .where(TokenSecurityScan.token_id == token.id)
        .order_by(TokenSecurityScan.scanned_at.desc())
        .limit(1)
    )
    holders = db.scalar(
        select(HolderAnalysis)
        .where(HolderAnalysis.token_id == token.id)
        .order_by(HolderAnalysis.scanned_at.desc())
        .limit(1)
    )
    liquidity = db.scalar(
        select(LiquidityAnalysisRecord)
        .where(LiquidityAnalysisRecord.token_id == token.id)
        .order_by(LiquidityAnalysisRecord.scanned_at.desc())
        .limit(1)
    )
    wallet = db.scalar(
        select(WalletActivity)
        .where(WalletActivity.token_id == token.id)
        .order_by(WalletActivity.scanned_at.desc())
        .limit(1)
    )
    t22 = db.scalar(
        select(Token2022ExtensionObservation)
        .where(Token2022ExtensionObservation.token_id == token.id)
        .order_by(Token2022ExtensionObservation.id.desc())
        .limit(1)
    )
    t22_types: list[str] = []
    if t22 is not None and t22.extension_types_json:
        try:
            loaded = json.loads(t22.extension_types_json)
            if isinstance(loaded, list):
                t22_types = [str(item) for item in loaded]
        except json.JSONDecodeError:
            t22_types = []
    return TokenSecurityOut(
        token_address=token.token_address,
        symbol=token.symbol,
        name=token.name,
        assessment=_assessment_out(assessment, settings),
        mint_authority_status=security.mint_authority_status if security else None,
        freeze_authority_status=security.freeze_authority_status if security else None,
        lock_status=liquidity.lock_status if liquidity else None,
        concentration_status=holders.concentration_status if holders else None,
        deployer_status=wallet.deployer_status if wallet else None,
        metadata_status=security.metadata_status if security else None,
        metadata_account=security.metadata_account if security else None,
        metadata_update_authority=security.metadata_update_authority if security else None,
        metadata_mutability=security.metadata_mutability if security else None,
        token_2022_inspection_status=t22.inspection_status if t22 else None,
        token_2022_extension_types=t22_types,
        token_2022_parser_complete=None if t22 is None else bool(t22.parser_complete),
        control_findings=[_finding_out(item) for item in findings_for_token(db, token).findings],
        observed_at=security.observed_at if security else assessment.observed_at,
        recorded_at=security.recorded_at if security else assessment.recorded_at,
        slot=security.slot if security else assessment.slot,
        source=security.source if security else assessment.source,
    )


def _finding_out(item) -> ControlFindingOut:
    return ControlFindingOut(
        code=item.code,
        severity=item.severity,
        status=item.status,
        explanation=item.explanation,
        evidence=item.evidence,
        source_observation_id=item.source_observation_id,
        source_observation_kind=item.source_observation_kind,
        source=item.source,
        observed_at=item.observed_at,
        slot=item.slot,
        confidence=item.confidence,
        limitations=item.limitations,
        verified=item.verified,
    )


def _classify_row(row: TokenRiskAssessment | RiskScoreHistory, settings: Settings) -> ClassificationView:
    return classify_measurements(
        settings,
        completeness=row.completeness or 0.0,
        confidence=row.confidence or 0.0,
        observed_risk=row.observed_risk,
        observed_risk_band=row.observed_risk_band,
        stored_risk_score=row.risk_score,
        stored_risk_band=row.risk_band,
    )


def _serialized_explanation(
    row: TokenRiskAssessment | RiskScoreHistory,
    classified: ClassificationView,
    settings: Settings,
    unknown: list[str],
) -> str:
    stored = row.explanation or ""
    if (
        classified.status == row.status
        and classified.risk_score == row.risk_score
        and classified.classification_reliable == (row.status == "scored")
    ):
        return stored
    return (
        f"{classified.status}: current thresholds require completeness "
        f">= {settings.security_min_completeness:.2f} and confidence "
        f">= {settings.security_min_confidence:.2f}. Stored completeness="
        f"{row.completeness:.2f}, confidence={row.confidence:.2f}. "
        f"Unknown categories are not treated as safe ({', '.join(unknown) or 'none'}). "
        f"Original stored note: {stored}"
    )


def _assessment_out(assessment: TokenRiskAssessment, settings: Settings) -> RiskAssessmentOut:
    try:
        warnings = json.loads(assessment.warnings_json or "[]")
    except json.JSONDecodeError:
        warnings = []
    categories = _category_models(assessment.category_scores_json)
    unknown = [item.name for item in categories if item.status == "UNKNOWN"]
    classified = _classify_row(assessment, settings)
    return RiskAssessmentOut(
        status=classified.status,
        observed_risk=classified.observed_risk,
        observed_risk_band=classified.observed_risk_band,
        risk_score=classified.risk_score,
        risk_band=classified.risk_band,
        confidence=assessment.confidence,
        completeness=assessment.completeness,
        classification_reliable=classified.classification_reliable,
        unknown_categories=unknown,
        explanation=_serialized_explanation(assessment, classified, settings, unknown),
        warnings=warnings if isinstance(warnings, list) else [],
        categories=categories,
        scanned_at=assessment.scanned_at,
    )


def _history_out(row: RiskScoreHistory, settings: Settings) -> RiskAssessmentOut:
    categories = _category_models(row.category_scores_json)
    unknown = [item.name for item in categories if item.status == "UNKNOWN"]
    classified = _classify_row(row, settings)
    return RiskAssessmentOut(
        status=classified.status,
        observed_risk=classified.observed_risk,
        observed_risk_band=classified.observed_risk_band,
        risk_score=classified.risk_score,
        risk_band=classified.risk_band,
        confidence=row.confidence,
        completeness=row.completeness,
        classification_reliable=classified.classification_reliable,
        unknown_categories=unknown,
        explanation=_serialized_explanation(row, classified, settings, unknown),
        warnings=[],
        categories=categories,
        scanned_at=row.scanned_at,
    )


def _category_models(raw: str | None) -> list[RiskCategoryOut]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    items: list[RiskCategoryOut] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        items.append(
            RiskCategoryOut(
                name=str(item.get("name") or ""),
                weight=float(item.get("weight") or 0),
                score=item.get("score"),
                status=str(item.get("status") or "UNKNOWN"),
                reason=str(item.get("reason") or ""),
            )
        )
    return items
