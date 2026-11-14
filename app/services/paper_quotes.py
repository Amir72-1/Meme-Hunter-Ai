"""Validate recorded USD token quotes for paper simulation. Never invent a price."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MarketSnapshot, PaperMarketQuote, Token
from app.services.paper_money import ZERO, PaperMoneyError, money
from app.services.paper_quote_service import (
    FRESHNESS_PROVIDER,
    STATUS_ACCEPTED,
    paper_quote_is_executable,
)
from app.timeutil import as_utc, utc_now

SOURCE = "market_snapshots.price_usd"
ON_DEMAND_SOURCE = "dexscreener_on_demand"


@dataclass
class PaperQuote:
    snapshot_id: int | None
    token_id: int
    mint_address: str
    price_usd: Decimal
    observed_at: datetime
    age_seconds: float
    liquidity_usd: Decimal | None
    source: str = SOURCE
    quote_id: int | None = None
    freshness_basis: str = "SNAPSHOT_RECORDED_AT"
    pair_address: str | None = None


class QuoteRejected(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _money_price(value: object, missing: str, invalid: str) -> Decimal:
    if value is None:
        raise QuoteRejected(missing, "No token price in USD. Market cap and FDV are not executable prices.")
    try:
        price = money(value)
    except PaperMoneyError as exc:
        raise QuoteRejected(invalid, str(exc)) from exc
    if price <= ZERO:
        raise QuoteRejected(invalid, "Token price must be greater than zero.")
    return price


def _money_liquidity(value: object, settings: Settings) -> Decimal:
    if value is None:
        raise QuoteRejected(
            "MISSING_LIQUIDITY",
            "Liquidity is missing. Missing liquidity is not treated as zero or as sufficient depth.",
        )
    try:
        liquidity = money(value)
    except PaperMoneyError as exc:
        raise QuoteRejected("INVALID_LIQUIDITY", str(exc)) from exc
    if liquidity <= ZERO:
        raise QuoteRejected(
            "INSUFFICIENT_LIQUIDITY",
            "Recorded liquidity is not positive. This is not treated as tradeable depth.",
        )
    minimum = money(settings.paper_min_liquidity_usd)
    if liquidity < minimum:
        raise QuoteRejected(
            "INSUFFICIENT_LIQUIDITY",
            f"Recorded liquidity {format(liquidity, 'f')} is below the paper minimum {format(minimum, 'f')}.",
        )
    return liquidity


def _age_or_stale(observed_at: datetime | None, now: datetime, settings: Settings, missing: str) -> float:
    if observed_at is None:
        raise QuoteRejected(missing, "Quote has no market observation timestamp.")
    age = max(0.0, (now - observed_at).total_seconds())
    max_age = float(settings.paper_max_quote_age_seconds)
    if age > max_age:
        raise QuoteRejected(
            "STALE_QUOTE",
            f"Quote age {age:.1f}s exceeds maximum {max_age:.1f}s.",
        )
    return age


def _from_on_demand(row: PaperMarketQuote, token: Token, settings: Settings, now: datetime) -> PaperQuote | None:
    if row.status != STATUS_ACCEPTED:
        return None
    if row.freshness_basis != FRESHNESS_PROVIDER or row.market_observed_at is None:
        return None
    if not paper_quote_is_executable(row, settings, now=now):
        return None
    observed_at = as_utc(row.market_observed_at)
    price = _money_price(row.price_usd, "MISSING_PRICE", "INVALID_PRICE")
    liquidity = _money_liquidity(row.liquidity_usd, settings)
    age = _age_or_stale(observed_at, now, settings, "MISSING_QUOTE_TIMESTAMP")
    return PaperQuote(
        snapshot_id=None,
        token_id=token.id,
        mint_address=token.token_address,
        price_usd=price,
        observed_at=observed_at,
        age_seconds=age,
        liquidity_usd=liquidity,
        source=row.source or ON_DEMAND_SOURCE,
        quote_id=row.id,
        freshness_basis=row.freshness_basis,
        pair_address=row.pair_address,
    )


def _from_snapshot(snapshot: MarketSnapshot, token: Token, settings: Settings, now: datetime) -> PaperQuote:
    observed_at = as_utc(snapshot.recorded_at)
    if observed_at is None:
        raise QuoteRejected("MISSING_QUOTE_TIMESTAMP", "Snapshot has no recorded_at timestamp.")
    price = _money_price(snapshot.price_usd, "MISSING_PRICE", "INVALID_PRICE")
    age = _age_or_stale(observed_at, now, settings, "MISSING_QUOTE_TIMESTAMP")
    liquidity = _money_liquidity(snapshot.liquidity_usd, settings)
    return PaperQuote(
        snapshot_id=snapshot.id,
        token_id=token.id,
        mint_address=token.token_address,
        price_usd=price,
        observed_at=observed_at,
        age_seconds=age,
        liquidity_usd=liquidity,
        source=SOURCE,
        freshness_basis="SNAPSHOT_RECORDED_AT",
    )


def _latest_on_demand(session: Session, token_id: int) -> PaperMarketQuote | None:
    return session.scalar(
        select(PaperMarketQuote)
        .where(PaperMarketQuote.token_id == token_id)
        .order_by(PaperMarketQuote.retrieved_at.desc(), PaperMarketQuote.id.desc())
        .limit(1)
    )


def _latest_snapshot(session: Session, token_id: int) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.token_id == token_id)
        .order_by(MarketSnapshot.recorded_at.desc(), MarketSnapshot.id.desc())
        .limit(1)
    )


def load_executable_quote(
    session: Session,
    token: Token,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> PaperQuote:
    now = as_utc(now or utc_now())
    on_demand = _latest_on_demand(session, token.id)
    if on_demand is not None:
        accepted = _from_on_demand(on_demand, token, settings, now)
        if accepted is not None:
            return accepted
    snapshot = _latest_snapshot(session, token.id)
    if snapshot is not None:
        return _from_snapshot(snapshot, token, settings, now)
    if on_demand is not None and on_demand.status == STATUS_ACCEPTED:
        if on_demand.freshness_basis != FRESHNESS_PROVIDER or on_demand.market_observed_at is None:
            raise QuoteRejected(
                "UNPROVEN_QUOTE_FRESHNESS",
                "On-demand retrieval time does not prove the underlying DexScreener price is fresh.",
            )
        observed = as_utc(on_demand.market_observed_at)
        _age_or_stale(observed, now, settings, "MISSING_QUOTE_TIMESTAMP")
        _money_price(on_demand.price_usd, "MISSING_PRICE", "INVALID_PRICE")
        _money_liquidity(on_demand.liquidity_usd, settings)
    if on_demand is not None and on_demand.status == "PROVIDER_UNAVAILABLE":
        raise QuoteRejected("PROVIDER_UNAVAILABLE", "DexScreener did not return a usable quote.")
    if on_demand is not None and on_demand.reject_reason:
        raise QuoteRejected(on_demand.reject_reason, on_demand.notes or on_demand.reject_reason)
    raise QuoteRejected("MISSING_QUOTE", "No recorded market snapshot exists for this token.")
