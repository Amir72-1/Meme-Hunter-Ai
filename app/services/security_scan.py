"""Orchestrate token security scans and persist historical assessments."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    HolderAnalysis,
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    RiskScoreHistory,
    Token,
    Token2022ExtensionObservation,
    TokenRiskAssessment,
    TokenSecurityScan,
    WalletActivity,
    utc_now,
)
from app.services.holder_analysis import analyze_holders
from app.services.liquidity_analysis import analyze_liquidity
from app.services.risk_scoring import RiskScoreResult, score_token_risk
from app.services.solana_rpc import SolanaRpcClient
from app.services.token_security import analyze_token_security
from app.services.trading_analysis import analyze_trading
from app.services.wallet_analysis import analyze_wallet_activity

logger = logging.getLogger("meme_hunter.security")


@dataclass
class SecurityScanSummary:
    status: str
    tokens_considered: int = 0
    tokens_scanned: int = 0
    skipped: int = 0
    insufficient_data: int = 0
    high_risk: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


async def scan_tokens(
    session: Session,
    rpc: SolanaRpcClient,
    settings: Settings | None = None,
    token_address: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> SecurityScanSummary:
    settings = settings or get_settings()
    summary = SecurityScanSummary(status="running", started_at=datetime.now(timezone.utc))
    scan_row = IntelligenceScan(scan_type="security", started_at=summary.started_at, status="running")
    session.add(scan_row)
    session.commit()

    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    if not token_address:
        tokens = tokens[: limit or settings.security_scan_limit]
    summary.tokens_considered = len(tokens)

    for token in tokens:
        if not force and _recently_scanned(session, token.id, settings.security_rescan_seconds):
            summary.skipped += 1
            continue
        try:
            assessment = await _scan_one(session, rpc, settings, token)
            summary.tokens_scanned += 1
            if assessment.status == "INSUFFICIENT_DATA":
                summary.insufficient_data += 1
            elif (
                assessment.status == "scored"
                and (assessment.risk_score or 0) >= settings.security_high_risk_threshold
            ):
                summary.high_risk += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Security scan failed for %s", token.token_address)
            summary.errors.append({"token_address": token.token_address, "detail": str(exc)})
            session.rollback()

    summary.status = "completed"
    summary.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = summary.finished_at
    scan_row.status = summary.status
    scan_row.items_fetched = summary.tokens_considered
    scan_row.items_saved = summary.tokens_scanned
    scan_row.duplicates_skipped = summary.skipped
    scan_row.details_json = json.dumps(
        {"insufficient_data": summary.insufficient_data, "high_risk": summary.high_risk}
    )
    scan_row.errors_json = json.dumps(summary.errors)
    session.commit()
    return summary


async def _scan_one(
    session: Session,
    rpc: SolanaRpcClient,
    settings: Settings,
    token: Token,
) -> TokenRiskAssessment:
    snapshots = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token.id)
            .order_by(MarketSnapshot.recorded_at.asc())
        ).all()
    )
    security = await analyze_token_security(token.token_address, rpc)
    holders = await analyze_holders(
        token.token_address,
        rpc,
        supply=security.supply,
        pair_address=token.pair_address,
    )
    liquidity = analyze_liquidity(token, snapshots)
    trading = analyze_trading(snapshots)
    wallet = analyze_wallet_activity(security)
    scored = score_token_risk(settings, security, holders, liquidity, trading, wallet)
    observed_at = security.observed_at or utc_now()
    recorded_at = utc_now()

    session.add(
        TokenSecurityScan(
            token_id=token.id,
            scanned_at=observed_at,
            source=security.source,
            observed_at=observed_at,
            recorded_at=recorded_at,
            slot=security.slot,
            provider=security.provider,
            mint_authority=security.mint_authority,
            mint_authority_status=security.mint_authority_status,
            freeze_authority=security.freeze_authority,
            freeze_authority_status=security.freeze_authority_status,
            token_program=security.token_program,
            token_standard=security.token_standard,
            supply=security.supply,
            decimals=security.decimals,
            metadata_mutability=security.metadata_mutability,
            metadata_status=security.metadata_status,
            metadata_account=security.metadata_account,
            metadata_update_authority=security.metadata_update_authority,
            metadata_name=security.metadata_name,
            metadata_symbol=security.metadata_symbol,
            metadata_uri=security.metadata_uri,
            metadata_is_mutable=None if security.metadata_is_mutable is None else int(security.metadata_is_mutable),
            findings_json=json.dumps(security.findings),
            errors_json=json.dumps(security.errors),
        )
    )
    _persist_token_2022_observation(session, token, security, recorded_at)
    session.add(
        HolderAnalysis(
            token_id=token.id,
            scanned_at=observed_at,
            provider=holders.provider,
            top_holder_pct=holders.top_holder_pct,
            top5_pct=holders.top5_pct,
            top10_pct=holders.top10_pct,
            observed_accounts=holders.observed_accounts,
            concentration_status=holders.concentration_status,
            holders_json=json.dumps([item.__dict__ for item in holders.holders]),
            errors_json=json.dumps(holders.errors),
        )
    )
    session.add(
        LiquidityAnalysisRecord(
            token_id=token.id,
            scanned_at=observed_at,
            provider=liquidity.provider,
            liquidity_usd=liquidity.liquidity_usd,
            pool_address=liquidity.pool_address,
            pool_age_hours=liquidity.pool_age_hours,
            liquidity_change_pct=liquidity.liquidity_change_pct,
            lock_status=liquidity.lock_status,
            lock_evidence=liquidity.lock_evidence,
            findings_json=json.dumps(liquidity.findings),
        )
    )
    session.add(
        WalletActivity(
            token_id=token.id,
            scanned_at=observed_at,
            provider=wallet.provider,
            deployer_address=wallet.deployer_address,
            deployer_status=wallet.deployer_status,
            findings_json=json.dumps(wallet.findings),
            errors_json=json.dumps(wallet.errors),
        )
    )
    assessment = TokenRiskAssessment(
        token_id=token.id,
        scanned_at=observed_at,
        source=security.source,
        observed_at=observed_at,
        recorded_at=recorded_at,
        slot=security.slot,
        status=scored.status,
        observed_risk=scored.observed_risk,
        observed_risk_band=scored.observed_risk_band,
        risk_score=scored.risk_score,
        risk_band=scored.risk_band,
        confidence=scored.confidence,
        completeness=scored.completeness,
        category_scores_json=_categories_json(scored),
        warnings_json=json.dumps(scored.warnings),
        explanation=scored.explanation,
    )
    session.add(assessment)
    session.add(
        RiskScoreHistory(
            token_id=token.id,
            scanned_at=observed_at,
            status=scored.status,
            observed_risk=scored.observed_risk,
            observed_risk_band=scored.observed_risk_band,
            risk_score=scored.risk_score,
            risk_band=scored.risk_band,
            confidence=scored.confidence,
            completeness=scored.completeness,
            category_scores_json=_categories_json(scored),
            explanation=scored.explanation,
        )
    )
    session.commit()
    session.refresh(assessment)
    return assessment


def latest_assessment(session: Session, token_id: int) -> TokenRiskAssessment | None:
    return session.scalar(
        select(TokenRiskAssessment)
        .where(TokenRiskAssessment.token_id == token_id)
        .order_by(TokenRiskAssessment.scanned_at.desc())
        .limit(1)
    )


def _recently_scanned(session: Session, token_id: int, rescan_seconds: int) -> bool:
    latest = latest_assessment(session, token_id)
    if latest is None:
        return False
    scanned = latest.scanned_at
    if scanned.tzinfo is None:
        scanned = scanned.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - scanned < timedelta(seconds=rescan_seconds)


def _categories_json(result: RiskScoreResult) -> str:
    return json.dumps(
        [
            {
                "name": item.name,
                "weight": item.weight,
                "score": item.score,
                "status": item.status,
                "reason": item.reason,
            }
            for item in result.categories
        ]
    )


def _persist_token_2022_observation(session: Session, token: Token, security, recorded_at) -> None:
    inspection = getattr(security, "token_2022_inspection", None)
    if inspection is None:
        return
    paused = inspection.pausable_paused
    session.add(
        Token2022ExtensionObservation(
            token_id=token.id,
            mint_address=inspection.mint,
            token_program=inspection.token_program or security.token_program,
            inspection_status=inspection.inspection_status,
            extension_types_json=json.dumps(inspection.extension_types),
            parsed_fields_json=json.dumps(inspection.parsed_fields),
            transfer_fee_bps=inspection.transfer_fee_bps,
            transfer_fee_max=inspection.transfer_fee_max,
            transfer_fee_config_authority=inspection.transfer_fee_config_authority,
            transfer_fee_withdraw_authority=inspection.transfer_fee_withdraw_authority,
            transfer_hook_program_id=inspection.transfer_hook_program_id,
            transfer_hook_authority=inspection.transfer_hook_authority,
            permanent_delegate=inspection.permanent_delegate,
            default_account_state=inspection.default_account_state,
            pausable_paused=None if paused is None else int(paused),
            pausable_authority=inspection.pausable_authority,
            mint_close_authority=inspection.mint_close_authority,
            confidential_transfer_present=(
                None
                if inspection.confidential_transfer_present is None
                else int(inspection.confidential_transfer_present)
            ),
            confidential_auto_approve=(
                None
                if inspection.confidential_auto_approve is None
                else int(inspection.confidential_auto_approve)
            ),
            metadata_pointer_address=inspection.metadata_pointer_address,
            metadata_pointer_authority=inspection.metadata_pointer_authority,
            token_metadata_present=(
                None
                if inspection.token_metadata_present is None
                else int(inspection.token_metadata_present)
            ),
            scaled_ui_amount_multiplier=inspection.scaled_ui_amount_multiplier,
            parser_complete=int(bool(inspection.parser_complete)),
            parser_notes=inspection.parser_notes,
            source=inspection.source,
            observed_at=inspection.observed_at,
            recorded_at=recorded_at,
            slot=inspection.slot,
            findings_json=json.dumps(inspection.findings),
            errors_json=json.dumps(inspection.errors),
        )
    )
