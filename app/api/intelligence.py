"""News, social, and narrative intelligence endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import IntelligenceScan, Narrative, NewsItem, Token, TokenNarrativeMatch
from app.schemas import (
    IntelligenceStatusResponse,
    NarrativeListResponse,
    NarrativeOut,
    NarrativeScanResponse,
    NewsItemOut,
    NewsListResponse,
    NewsScanResponse,
    SocialAdapterStatusOut,
    SocialScanResponse,
    TokenNarrativeMatchOut,
)
from app.services.news_collector import collect_news
from app.services.social_adapters import collect_social
from app.services.narrative_engine import detect_and_score_narratives
from app.services.token_narrative_matcher import match_tokens_to_narratives

router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])


@router.post("/news/scan", response_model=NewsScanResponse)
async def news_scan(db: Session = Depends(get_db)) -> NewsScanResponse | JSONResponse:
    result = await collect_news(db)
    payload = NewsScanResponse(
        status=result.status,
        feeds_attempted=result.feeds_attempted,
        feeds_ok=result.feeds_ok,
        items_fetched=result.items_fetched,
        items_saved=result.items_saved,
        duplicates_skipped=result.duplicates_skipped,
        started_at=result.started_at,
        finished_at=result.finished_at,
        errors=result.errors,
        skipped_feeds=result.skipped_feeds,
    )
    if result.status == "failed":
        return JSONResponse(status_code=502, content=payload.model_dump(mode="json"))
    return payload


@router.post("/social/scan", response_model=SocialScanResponse)
async def social_scan(db: Session = Depends(get_db)) -> SocialScanResponse:
    result = await collect_social(db)
    return SocialScanResponse(
        status=result.status,
        items_saved=result.items_saved,
        duplicates_skipped=result.duplicates_skipped,
        adapters=[SocialAdapterStatusOut(**item.__dict__) for item in result.adapters],
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


@router.post("/narratives/scan", response_model=NarrativeScanResponse)
def narratives_scan(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> NarrativeScanResponse:
    detected = detect_and_score_narratives(db, settings)
    matched = match_tokens_to_narratives(db, settings)
    return NarrativeScanResponse(
        status=detected.status,
        narratives_upserted=detected.narratives_upserted,
        mentions_saved=detected.mentions_saved,
        scores_saved=detected.scores_saved,
        matches_saved=matched.matches_saved,
        weak_matches_skipped=matched.weak_skipped,
        started_at=detected.started_at,
        finished_at=matched.finished_at or detected.finished_at,
        errors=detected.errors + matched.errors,
    )


@router.get("/news", response_model=NewsListResponse)
def list_news(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category: str | None = None,
) -> NewsListResponse:
    stmt = select(NewsItem)
    if category:
        stmt = stmt.where(NewsItem.category == category)
    items = list(db.scalars(stmt.order_by(NewsItem.published_at.desc(), NewsItem.id.desc())).all())
    total = len(items)
    page = items[offset : offset + limit]
    return NewsListResponse(
        items=[NewsItemOut.model_validate(item) for item in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/narratives", response_model=NarrativeListResponse)
def list_narratives(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> NarrativeListResponse:
    items = list(
        db.scalars(select(Narrative).order_by(Narrative.trend_score.desc(), Narrative.id.desc())).all()
    )
    total = len(items)
    page = items[offset : offset + limit]
    return NarrativeListResponse(
        items=[_narrative_out(db, item) for item in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/narratives/{narrative_id}", response_model=NarrativeOut)
def get_narrative(narrative_id: int, db: Session = Depends(get_db)) -> NarrativeOut:
    narrative = db.get(Narrative, narrative_id)
    if narrative is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Narrative not found")
    return _narrative_out(db, narrative)


@router.get("/status", response_model=IntelligenceStatusResponse)
def intelligence_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IntelligenceStatusResponse:
    return IntelligenceStatusResponse(
        news=_latest_scan(db, "news") | {"feeds_configured": True},
        social=_latest_scan(db, "social")
        | {
            "reddit": "configured" if settings.reddit_client_id else "NOT_CONFIGURED",
            "x": "configured" if settings.x_bearer_token else "NOT_CONFIGURED",
            "telegram": "configured" if settings.telegram_bot_token else "NOT_CONFIGURED",
        },
        narratives=_latest_scan(db, "narratives"),
        note=(
            "News uses public RSS. Social adapters stay NOT_CONFIGURED without credentials. "
            "Narrative scores are computed from stored sources only. "
            "A token name containing a keyword is not treated as a confirmed link."
        ),
    )


def _latest_scan(db: Session, scan_type: str) -> dict:
    row = db.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == scan_type)
        .order_by(IntelligenceScan.started_at.desc())
        .limit(1)
    )
    if row is None:
        return {"last_scan_status": "never", "last_scan_at": None}
    stamp = row.finished_at or row.started_at
    return {
        "last_scan_status": row.status,
        "last_scan_at": stamp.isoformat() if stamp else None,
        "items_saved": row.items_saved,
    }


def _narrative_out(db: Session, narrative: Narrative) -> NarrativeOut:
    keywords: list[str] = []
    if narrative.keywords:
        try:
            loaded = json.loads(narrative.keywords)
            if isinstance(loaded, list):
                keywords = [str(item) for item in loaded]
        except json.JSONDecodeError:
            keywords = []
    matches = list(
        db.scalars(
            select(TokenNarrativeMatch).where(TokenNarrativeMatch.narrative_id == narrative.id)
        ).all()
    )
    token_ids = [match.token_id for match in matches]
    tokens = {
        token.id: token
        for token in db.scalars(select(Token).where(Token.id.in_(token_ids))).all()
    } if token_ids else {}
    match_out = []
    for match in matches:
        token = tokens.get(match.token_id)
        try:
            match_keywords = json.loads(match.matching_keywords or "[]")
        except json.JSONDecodeError:
            match_keywords = []
        try:
            refs = json.loads(match.source_refs or "[]")
        except json.JSONDecodeError:
            refs = []
        match_out.append(
            TokenNarrativeMatchOut(
                token_address=token.token_address if token else None,
                token_symbol=token.symbol if token else None,
                token_name=token.name if token else None,
                narrative_name=narrative.name,
                matching_keywords=match_keywords if isinstance(match_keywords, list) else [],
                explanation=match.explanation,
                confidence=match.confidence,
                method=match.method,
                source_refs=refs if isinstance(refs, list) else [],
            )
        )
    return NarrativeOut(
        id=narrative.id,
        name=narrative.name,
        category=narrative.category,
        description=narrative.description,
        trend_score=narrative.trend_score,
        mention_count=narrative.mention_count,
        mention_growth=narrative.mention_growth,
        engagement_growth=narrative.engagement_growth,
        independent_sources=narrative.independent_sources,
        independent_authors=narrative.independent_authors,
        recency_score=narrative.recency_score,
        acceleration=narrative.acceleration,
        persistence=narrative.persistence,
        spam_score=narrative.spam_score,
        confidence=narrative.confidence,
        data_completeness=narrative.data_completeness,
        explanation=narrative.explanation,
        keywords=keywords,
        first_detected_at=narrative.first_detected_at,
        last_detected_at=narrative.last_detected_at,
        matches=match_out,
    )
