"""Scan stored tokens and persist historical opportunity assessments. No live RPC."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    Narrative,
    OpportunityAssessment,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
)
from app.services.opportunity_scoring import (
    HIGH_OPPORTUNITY,
    INSUFFICIENT_DATA,
    LOW_CONFIDENCE,
    LOW_OPPORTUNITY,
    SCORED,
    WATCHLIST,
    OpportunityResult,
    assess_opportunity,
    snapshots_as_of,
)
from app.services.token_narrative_matcher import match_tokens_to_narratives

logger = logging.getLogger("meme_hunter.opportunity")


@dataclass
class OpportunityScanSummary:
    status: str
    tokens_considered: int = 0
    tokens_scanned: int = 0
    skipped: int = 0
    scored: int = 0
    low_confidence: int = 0
    insufficient_data: int = 0
    high_opportunity: int = 0
    watchlist: int = 0
    low_opportunity: int = 0
    reliable_classifications: int = 0
    tokens_with_narrative_matches: int = 0
    tokens_with_confirmed_security: int = 0
    tokens_with_single_snapshot: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


def scan_opportunities(
    session: Session,
    settings: Settings | None = None,
    token_address: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> OpportunityScanSummary:
    settings = settings or get_settings()
    summary = OpportunityScanSummary(status="running", started_at=datetime.now(timezone.utc))
    scan_row = IntelligenceScan(scan_type="opportunity", started_at=summary.started_at, status="running")
    session.add(scan_row)
    session.commit()
    scan_id = scan_row.id

    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    if not token_address:
        tokens = tokens[: limit or settings.opportunity_scan_limit]
    summary.tokens_considered = len(tokens)
    match_tokens_to_narratives(session, settings)

    for token in tokens:
        latest = latest_opportunity(session, token.id)
        if (
            not force
            and _recently_assessed(latest, settings.opportunity_rescan_seconds)
            and not assessment_is_stale(session, token.id, latest)
        ):
            summary.skipped += 1
            continue
        try:
            result = assess_stored_token(session, settings, token)
            _persist(session, token.id, result)
            summary.tokens_scanned += 1
            if result.status == SCORED:
                summary.scored += 1
                summary.reliable_classifications += 1
            elif result.status == LOW_CONFIDENCE:
                summary.low_confidence += 1
            elif result.status == INSUFFICIENT_DATA:
                summary.insufficient_data += 1
            if result.ranking_label == HIGH_OPPORTUNITY:
                summary.high_opportunity += 1
            elif result.ranking_label == WATCHLIST:
                summary.watchlist += 1
            elif result.ranking_label == LOW_OPPORTUNITY:
                summary.low_opportunity += 1
            if result.narrative_match_count:
                summary.tokens_with_narrative_matches += 1
            if result.security_coverage == "CONFIRMED":
                summary.tokens_with_confirmed_security += 1
            if result.snapshot_count == 1:
                summary.tokens_with_single_snapshot += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Opportunity assessment failed for %s", token.token_address)
            summary.errors.append({"token_address": token.token_address, "detail": str(exc)})
            session.rollback()

    summary.status = "completed"
    summary.finished_at = datetime.now(timezone.utc)
    scan_row = session.get(IntelligenceScan, scan_id) or scan_row
    scan_row.finished_at = summary.finished_at
    scan_row.status = summary.status
    scan_row.items_fetched = summary.tokens_considered
    scan_row.items_saved = summary.tokens_scanned
    scan_row.duplicates_skipped = summary.skipped
    scan_row.details_json = json.dumps(
        {
            "scored": summary.scored,
            "low_confidence": summary.low_confidence,
            "insufficient_data": summary.insufficient_data,
            "high_opportunity": summary.high_opportunity,
            "watchlist": summary.watchlist,
            "low_opportunity": summary.low_opportunity,
            "reliable_classifications": summary.reliable_classifications,
            "tokens_with_narrative_matches": summary.tokens_with_narrative_matches,
            "tokens_with_confirmed_security": summary.tokens_with_confirmed_security,
            "tokens_with_single_snapshot": summary.tokens_with_single_snapshot,
        }
    )
    scan_row.errors_json = json.dumps(summary.errors)
    session.commit()
    return summary


def assess_stored_token(session: Session, settings: Settings, token: Token) -> OpportunityResult:
    snapshots = load_usable_snapshots(session, token.id)
    match_rows = list(
        session.scalars(
            select(TokenNarrativeMatch).where(TokenNarrativeMatch.token_id == token.id)
        ).all()
    )
    if match_rows:
        narratives = {
            item.id: item
            for item in session.scalars(
                select(Narrative).where(Narrative.id.in_([row.narrative_id for row in match_rows]))
            ).all()
        }
    else:
        narratives = {}
    matches = [(row, narratives.get(row.narrative_id)) for row in match_rows]
    risk = session.scalar(
        select(TokenRiskAssessment)
        .where(TokenRiskAssessment.token_id == token.id)
        .order_by(TokenRiskAssessment.scanned_at.desc())
        .limit(1)
    )
    liquidity = session.scalar(
        select(LiquidityAnalysisRecord)
        .where(LiquidityAnalysisRecord.token_id == token.id)
        .order_by(LiquidityAnalysisRecord.scanned_at.desc())
        .limit(1)
    )
    return assess_opportunity(settings, token, snapshots, matches, risk, liquidity)


def load_usable_snapshots(session: Session, token_id: int, now: datetime | None = None) -> list[MarketSnapshot]:
    now = now or datetime.now(timezone.utc)
    rows = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token_id)
            .order_by(MarketSnapshot.recorded_at.asc(), MarketSnapshot.id.asc())
        ).all()
    )
    return snapshots_as_of(rows, now)


def assessment_is_stale(
    session: Session,
    token_id: int,
    assessment: OpportunityAssessment | None,
    now: datetime | None = None,
) -> tuple[bool, str | None, int, datetime | None]:
    now = now or datetime.now(timezone.utc)
    snapshots = load_usable_snapshots(session, token_id, now)
    current_count = len(snapshots)
    last_snapshot_at = snapshots[-1].recorded_at if snapshots else None
    if assessment is None:
        return True, "No persisted opportunity assessment.", current_count, last_snapshot_at
    return describe_staleness(assessment, current_count, last_snapshot_at, now) + (
        current_count,
        last_snapshot_at,
    )


def describe_staleness(
    assessment: OpportunityAssessment,
    current_count: int,
    last_snapshot_at: datetime | None,
    now: datetime,
) -> tuple[bool, str | None]:
    assessed_at = _aware(assessment.assessed_at) or now
    last_at = _aware(last_snapshot_at)
    stored_count = assessment.snapshot_count
    if stored_count is None and current_count > 0:
        return True, "Persisted assessment has no snapshot_count; newer market observations may exist."
    if stored_count is not None and stored_count != current_count:
        return True, (
            f"Persisted assessment used {stored_count} snapshot(s); "
            f"{current_count} usable snapshot(s) are stored now."
        )
    if last_at is not None and last_at > assessed_at:
        return True, "A market snapshot was recorded after this assessment."
    return False, None


def latest_opportunity(session: Session, token_id: int) -> OpportunityAssessment | None:
    return session.scalar(
        select(OpportunityAssessment)
        .where(OpportunityAssessment.token_id == token_id)
        .order_by(OpportunityAssessment.assessed_at.desc())
        .limit(1)
    )


def _persist(session: Session, token_id: int, result: OpportunityResult) -> OpportunityAssessment:
    row = OpportunityAssessment(
        token_id=token_id,
        assessed_at=result.assessed_at,
        status=result.status,
        ranking_label=result.ranking_label,
        opportunity_score=result.opportunity_score,
        positive_opportunity_score=result.positive_opportunity_score,
        security_adjustment=result.security_adjustment,
        confidence=result.confidence,
        data_completeness=result.data_completeness,
        available_components_json=json.dumps(result.available_components),
        missing_components_json=json.dumps(result.missing_components),
        component_scores_json=_components_json(result),
        positive_signals_json=json.dumps(result.positive_signals),
        negative_signals_json=json.dumps(result.negative_signals),
        warnings_json=json.dumps(result.warnings),
        explanation=result.explanation,
        snapshot_count=result.snapshot_count,
        last_snapshot_at=result.last_snapshot_at,
        snapshot_age_hours=result.snapshot_age_hours,
        narrative_match_count=result.narrative_match_count,
        security_coverage=result.security_coverage,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _recently_assessed(assessment: OpportunityAssessment | None, rescan_seconds: int) -> bool:
    if assessment is None:
        return False
    scanned = _aware(assessment.assessed_at)
    if scanned is None:
        return False
    return datetime.now(timezone.utc) - scanned < timedelta(seconds=rescan_seconds)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _components_json(result: OpportunityResult) -> str:
    return json.dumps(
        [
            {
                "name": item.name,
                "weight": item.weight,
                "score": item.score,
                "confidence": item.confidence,
                "status": item.status,
                "explanation": item.explanation,
                "metrics": item.metrics,
            }
            for item in result.components
        ]
    )
