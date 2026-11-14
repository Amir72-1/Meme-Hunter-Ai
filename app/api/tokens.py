"""Discovered token query endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketSnapshot, Token
from app.schemas import (
    MarketSnapshotOut,
    TokenDetailResponse,
    TokenHistoryResponse,
    TokenListResponse,
    TokenSummaryOut,
)

router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"])


def _latest_snapshots(db: Session, token_ids: list[int]) -> dict[int, MarketSnapshot]:
    if not token_ids:
        return {}
    snapshots = db.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.token_id.in_(token_ids))
        .order_by(MarketSnapshot.recorded_at.desc(), MarketSnapshot.id.desc())
    ).all()
    latest: dict[int, MarketSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.token_id not in latest:
            latest[snapshot.token_id] = snapshot
    return latest


def _summary(token: Token, snapshot: MarketSnapshot | None) -> TokenSummaryOut:
    return TokenSummaryOut(
        token_address=token.token_address,
        name=token.name,
        symbol=token.symbol,
        chain=token.chain,
        dex_name=token.dex_name,
        pair_address=token.pair_address,
        pair_created_at=token.pair_created_at,
        discovery_source=token.discovery_source,
        last_discovered_at=token.last_discovered_at,
        filter_status=token.filter_status,
        filter_reasons=token.filter_reasons,
        latest_snapshot=MarketSnapshotOut.model_validate(snapshot) if snapshot else None,
        safety_status="unassessed",
    )


@router.get("", response_model=TokenListResponse)
def list_tokens(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    min_liquidity: float | None = Query(default=None, ge=0),
    min_volume: float | None = Query(default=None, ge=0),
    max_age_hours: float | None = Query(default=None, ge=0),
    discovery_source: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Search symbol, name, or token address"),
) -> TokenListResponse:
    stmt = select(Token)
    if discovery_source:
        stmt = stmt.where(Token.discovery_source.contains(discovery_source))
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Token.name).like(pattern),
                func.lower(Token.symbol).like(pattern),
                func.lower(Token.token_address).like(pattern),
            )
        )
    if max_age_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        stmt = stmt.where(Token.pair_created_at.is_not(None), Token.pair_created_at >= cutoff)

    tokens = list(db.scalars(stmt.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    latest = _latest_snapshots(db, [token.id for token in tokens])

    filtered: list[tuple[Token, MarketSnapshot | None]] = []
    for token in tokens:
        snapshot = latest.get(token.id)
        liquidity = snapshot.liquidity_usd if snapshot is not None else None
        volume = snapshot.volume_24h if snapshot is not None else None
        if min_liquidity is not None and (liquidity is None or liquidity < min_liquidity):
            continue
        if min_volume is not None and (volume is None or volume < min_volume):
            continue
        filtered.append((token, snapshot))

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return TokenListResponse(
        items=[_summary(token, snapshot) for token, snapshot in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{token_address}", response_model=TokenDetailResponse)
def get_token(token_address: str, db: Session = Depends(get_db)) -> TokenDetailResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    latest = _latest_snapshots(db, [token.id]).get(token.id)
    summary = _summary(token, latest)
    return TokenDetailResponse(
        **summary.model_dump(),
        id=token.id,
        created_at=token.created_at,
        updated_at=token.updated_at,
    )


@router.get("/{token_address}/history", response_model=TokenHistoryResponse)
def get_token_history(token_address: str, db: Session = Depends(get_db)) -> TokenHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == token_address))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    snapshots = db.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.token_id == token.id)
        .order_by(MarketSnapshot.recorded_at.asc(), MarketSnapshot.id.asc())
    ).all()
    return TokenHistoryResponse(
        token_address=token.token_address,
        items=[MarketSnapshotOut.model_validate(item) for item in snapshots],
    )
