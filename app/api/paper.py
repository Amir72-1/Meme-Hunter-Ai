"""Paper-trading read APIs and a manual simulation endpoint. No automatic orders."""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import PaperDecision, PaperOrder, PaperTrade
from app.schemas import (
    PaperDecisionOut,
    PaperDecisionScanRequest,
    PaperDecisionScanResponse,
    PaperLimitsOut,
    PaperOpportunityDecisionOut,
    PaperOrderOut,
    PaperPortfolioResponse,
    PaperPositionOut,
    PaperQuoteOut,
    PaperSimulateRequest,
    PaperStatusResponse,
    PaperTradeOut,
)
from app.services.paper_decision_engine import (
    DecisionRequestError,
    decision_public,
    evaluate_and_store,
    get_opportunity_decision,
    scan_decisions,
)
from app.services.paper_quote_service import (
    QuoteRequestError,
    get_latest_stored_quote,
    quote_to_public,
    refresh_paper_quote,
)
from app.services.paper_money import money_text
from app.services.paper_trading import (
    build_portfolio_view,
    ensure_default_portfolio,
    simulate_manual_order,
)

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])


def _limits(settings: Settings) -> PaperLimitsOut:
    return PaperLimitsOut(
        max_allocation_pct=settings.paper_max_allocation_pct,
        max_exposure_pct=settings.paper_max_exposure_pct,
        max_daily_realized_loss_usd=settings.paper_max_daily_realized_loss_usd,
        max_open_positions=settings.paper_max_open_positions,
        min_liquidity_usd=settings.paper_min_liquidity_usd,
        max_quote_age_seconds=settings.paper_max_quote_age_seconds,
        fee_bps=settings.paper_fee_bps,
        slippage_bps=settings.paper_slippage_bps,
        max_slippage_bps=settings.paper_max_slippage_bps,
    )


def _order_out(order: PaperOrder) -> PaperOrderOut:
    assumptions = json.loads(order.assumptions_json) if order.assumptions_json else None
    return PaperOrderOut(
        id=order.id,
        client_order_id=order.client_order_id,
        mint_address=order.mint_address,
        side=order.side,
        status=order.status,
        requested_notional_usd=order.requested_notional_usd,
        requested_quantity=order.requested_quantity,
        quote_snapshot_id=order.quote_snapshot_id,
        paper_quote_id=order.paper_quote_id,
        observed_price_usd=order.observed_price_usd,
        observed_at=order.observed_at,
        simulated_price_usd=order.simulated_price_usd,
        fill_quantity=order.fill_quantity,
        fill_notional_usd=order.fill_notional_usd,
        fee_usd=order.fee_usd,
        slippage_usd=order.slippage_usd,
        reject_reason=order.reject_reason,
        assumptions=assumptions,
        created_at=order.created_at,
        filled_at=order.filled_at,
    )


@router.get("/portfolio", response_model=PaperPortfolioResponse)
def get_portfolio(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperPortfolioResponse:
    portfolio = ensure_default_portfolio(db, settings)
    view = build_portfolio_view(db, portfolio, settings)
    return PaperPortfolioResponse(
        id=portfolio.id,
        name=portfolio.name,
        mode=portfolio.mode,
        currency=portfolio.currency,
        starting_cash_usd=portfolio.starting_cash_usd,
        cash_available_usd=money_text(view.cash_available),
        cash_reserved_usd=money_text(view.cash_reserved),
        realized_pnl_usd=money_text(view.realized_pnl),
        fees_paid_usd=portfolio.fees_paid_usd,
        slippage_paid_usd=portfolio.slippage_paid_usd,
        unrealized_pnl_usd=money_text(view.unrealized_pnl) if view.unrealized_pnl is not None else None,
        positions_market_value_usd=(
            money_text(view.positions_market_value) if view.positions_market_value is not None else None
        ),
        equity_usd=money_text(view.equity) if view.equity is not None else None,
        valuation_status=view.valuation_status,
        unknown_positions=view.unknown_positions,
        positions=[PaperPositionOut(**item) for item in view.positions],
        note=(
            "PAPER_ONLY simulated book. Cash is USD. Equity is UNKNOWN when any open position "
            "lacks a fresh valid USD quote. No live wallet."
        ),
    )


@router.get("/positions")
def get_positions(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    portfolio = ensure_default_portfolio(db, settings)
    view = build_portfolio_view(db, portfolio, settings)
    return {
        "items": view.positions,
        "valuation_status": view.valuation_status,
        "note": "Open and flat paper positions. Missing quotes are UNKNOWN, not zero.",
    }


@router.get("/orders")
def get_orders(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    portfolio = ensure_default_portfolio(db, settings)
    rows = list(
        db.scalars(
            select(PaperOrder)
            .where(PaperOrder.portfolio_id == portfolio.id)
            .order_by(PaperOrder.id.desc())
            .limit(limit)
        ).all()
    )
    return {
        "items": [_order_out(row).model_dump() for row in rows],
        "note": "Append-only simulated orders. FILLED is not on-chain execution.",
    }


@router.get("/trades")
def get_trades(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    portfolio = ensure_default_portfolio(db, settings)
    rows = list(
        db.scalars(
            select(PaperTrade)
            .where(PaperTrade.portfolio_id == portfolio.id)
            .order_by(PaperTrade.id.desc())
            .limit(limit)
        ).all()
    )
    items = [
        PaperTradeOut(
            id=row.id,
            order_id=row.order_id,
            mint_address=row.mint_address,
            side=row.side,
            quantity=row.quantity,
            observed_price_usd=row.observed_price_usd,
            simulated_price_usd=row.simulated_price_usd,
            notional_usd=row.notional_usd,
            fee_usd=row.fee_usd,
            slippage_usd=row.slippage_usd,
            realized_pnl_usd=row.realized_pnl_usd,
            quote_snapshot_id=row.quote_snapshot_id,
            created_at=row.created_at,
        ).model_dump()
        for row in rows
    ]
    return {"items": items, "note": "Simulated fills. Observed price and simulated execution price are stored separately."}


@router.get("/decisions")
def get_decisions(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    portfolio = ensure_default_portfolio(db, settings)
    rows = list(
        db.scalars(
            select(PaperDecision)
            .where(PaperDecision.portfolio_id == portfolio.id)
            .order_by(PaperDecision.id.desc())
            .limit(limit)
        ).all()
    )
    items = [
        PaperDecisionOut(
            id=row.id,
            mint_address=row.mint_address,
            order_id=row.order_id,
            decision_type=row.decision_type,
            outcome=row.outcome,
            reason=row.reason,
            details=json.loads(row.details_json) if row.details_json else None,
            created_at=row.created_at,
        ).model_dump()
        for row in rows
    ]
    return {
        "items": items,
        "note": "Paper decision log including opportunity evaluations. No automatic orders.",
    }


@router.post("/decisions/evaluate/{token_address}", response_model=PaperOpportunityDecisionOut)
def evaluate_opportunity_decision(
    token_address: str,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperOpportunityDecisionOut:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    try:
        row, _skipped = evaluate_and_store(db, token_address, settings, force=force)
    except DecisionRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PaperOpportunityDecisionOut(**decision_public(row))


@router.post("/decisions/scan", response_model=PaperDecisionScanResponse)
def scan_opportunity_decisions(
    payload: PaperDecisionScanRequest = Body(default_factory=PaperDecisionScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperDecisionScanResponse:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    summary = scan_decisions(
        db,
        settings,
        limit=payload.limit,
        force=payload.force,
        token_addresses=payload.token_addresses,
    )
    return PaperDecisionScanResponse(
        status=summary.status,
        scan_id=summary.scan_id,
        tokens_considered=summary.tokens_considered,
        tokens_evaluated=summary.tokens_evaluated,
        skipped=summary.skipped,
        buy_candidate=summary.buy_candidate,
        wait=summary.wait,
        reject=summary.reject,
        orders_created=0,
        automatic_execution=False,
        external_provider_calls=0,
        decisions=summary.decisions,
        errors=summary.errors,
        note=summary.note,
    )


@router.get("/decisions/{decision_id}", response_model=PaperOpportunityDecisionOut)
def get_opportunity_decision_row(
    decision_id: int,
    db: Session = Depends(get_db),
) -> PaperOpportunityDecisionOut:
    try:
        row = get_opportunity_decision(db, decision_id)
    except DecisionRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PaperOpportunityDecisionOut(**decision_public(row))


@router.get("/status", response_model=PaperStatusResponse)
def paper_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperStatusResponse:
    portfolio = ensure_default_portfolio(db, settings)
    view = build_portfolio_view(db, portfolio, settings)
    return PaperStatusResponse(
        mode="PAPER_ONLY",
        auto_trading=False,
        live_trading=False,
        wallet_connected=False,
        automatic_orders=False,
        starting_cash_usd=portfolio.starting_cash_usd,
        cash_available_usd=money_text(view.cash_available),
        equity_usd=money_text(view.equity) if view.equity is not None else None,
        valuation_status=view.valuation_status,
        limits=_limits(settings),
        note=(
            "PAPER_ONLY. Manual simulation only. On-demand quotes are stored separately "
            "from discovery snapshots. Automatic buy/sell is not enabled. No wallet or private keys."
        ),
    )


@router.post("/simulate", response_model=PaperOrderOut)
def simulate_order(
    payload: PaperSimulateRequest = Body(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperOrderOut:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    side = payload.side.upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="side must be BUY or SELL")
    order = simulate_manual_order(
        db,
        settings,
        mint_address=payload.token_address,
        side=side,
        quantity_value=payload.quantity,
        notional_usd=payload.notional_usd,
        client_order_id=payload.client_order_id,
    )
    return _order_out(order)


def _quote_http(exc: QuoteRequestError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/quotes/refresh/{token_address}", response_model=PaperQuoteOut)
async def refresh_stored_quote(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperQuoteOut:
    if not settings.paper_trading_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Paper trading is disabled")
    try:
        row = await refresh_paper_quote(db, token_address, settings)
    except QuoteRequestError as exc:
        raise _quote_http(exc) from exc
    payload = quote_to_public(row, settings)
    payload["external_request"] = not payload["cache_hit"]
    return PaperQuoteOut(**payload)


@router.get("/quotes/{token_address}", response_model=PaperQuoteOut)
def get_stored_quote(
    token_address: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PaperQuoteOut:
    try:
        row = get_latest_stored_quote(db, token_address)
    except QuoteRequestError as exc:
        raise _quote_http(exc) from exc
    payload = quote_to_public(row, settings)
    payload["external_request"] = False
    return PaperQuoteOut(**payload)
