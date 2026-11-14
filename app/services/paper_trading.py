"""Paper portfolio ledger. PAPER_ONLY; no wallets, keys, or chain submits."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    PaperDecision,
    PaperEquitySnapshot,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperTrade,
    Token,
    utc_now,
)
from app.services.paper_money import ZERO, money, money_text, qty_text, quantity
from app.services.paper_quotes import QuoteRejected, load_executable_quote
from app.services.paper_simulator import (
    SIDE_BUY,
    SIDE_SELL,
    FillPlan,
    SimulationRejected,
    plan_fill,
)
from app.timeutil import as_utc

DEFAULT_PORTFOLIO = "default"
MODE = "PAPER_ONLY"


class PaperTradingError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class PortfolioView:
    portfolio: PaperPortfolio
    positions: list[dict]
    cash_available: Decimal
    cash_reserved: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    positions_market_value: Decimal | None
    equity: Decimal | None
    valuation_status: str
    unknown_positions: list[str]


def ensure_default_portfolio(session: Session, settings: Settings | None = None) -> PaperPortfolio:
    settings = settings or get_settings()
    existing = session.scalar(select(PaperPortfolio).where(PaperPortfolio.name == DEFAULT_PORTFOLIO))
    if existing is not None:
        return existing
    starting = money(settings.paper_starting_balance)
    now = utc_now()
    row = PaperPortfolio(
        name=DEFAULT_PORTFOLIO,
        mode=MODE,
        currency="USD",
        starting_cash_usd=money_text(starting),
        cash_available_usd=money_text(starting),
        cash_reserved_usd=money_text(ZERO),
        realized_pnl_usd=money_text(ZERO),
        fees_paid_usd=money_text(ZERO),
        slippage_paid_usd=money_text(ZERO),
        created_at=now,
        updated_at=now,
        note="Simulated paper book. No live wallet, private keys, or blockchain transactions.",
    )
    session.add(row)
    session.flush()
    session.add(
        PaperDecision(
            portfolio_id=row.id,
            decision_type="INIT",
            outcome="ACCEPTED",
            reason="PORTFOLIO_INITIALIZED",
            details_json=json.dumps(
                {
                    "starting_cash_usd": money_text(starting),
                    "mode": MODE,
                    "auto_trading": False,
                }
            ),
        )
    )
    session.add(
        PaperEquitySnapshot(
            portfolio_id=row.id,
            cash_available_usd=money_text(starting),
            cash_reserved_usd=money_text(ZERO),
            positions_market_value_usd=money_text(ZERO),
            unrealized_pnl_usd=money_text(ZERO),
            realized_pnl_usd=money_text(ZERO),
            equity_usd=money_text(starting),
            valuation_status="KNOWN",
            assumptions_json=json.dumps({"positions": []}),
            recorded_at=now,
        )
    )
    session.commit()
    session.refresh(row)
    return row


def get_default_portfolio(session: Session) -> PaperPortfolio | None:
    return session.scalar(select(PaperPortfolio).where(PaperPortfolio.name == DEFAULT_PORTFOLIO))


def _positions(session: Session, portfolio_id: int) -> list[PaperPosition]:
    return list(
        session.scalars(
            select(PaperPosition)
            .where(PaperPosition.portfolio_id == portfolio_id)
            .order_by(PaperPosition.id.asc())
        ).all()
    )


def _open_positions(rows: list[PaperPosition]) -> list[PaperPosition]:
    return [row for row in rows if quantity(row.quantity) > ZERO]


def _daily_realized_loss(session: Session, portfolio_id: int, now: datetime) -> Decimal:
    start = as_utc(now) - timedelta(days=1)
    total = ZERO
    trades = session.scalars(
        select(PaperTrade).where(
            PaperTrade.portfolio_id == portfolio_id,
            PaperTrade.created_at >= start,
        )
    )
    for trade in trades:
        pnl = money(trade.realized_pnl_usd)
        if pnl < ZERO:
            total += pnl
    return money(total)


def record_decision(
    session: Session,
    portfolio: PaperPortfolio,
    *,
    decision_type: str,
    outcome: str,
    reason: str,
    mint: str | None = None,
    token_id: int | None = None,
    order_id: int | None = None,
    details: dict | None = None,
) -> PaperDecision:
    row = PaperDecision(
        portfolio_id=portfolio.id,
        token_id=token_id,
        mint_address=mint,
        order_id=order_id,
        decision_type=decision_type,
        outcome=outcome,
        reason=reason,
        details_json=json.dumps(details or {}),
    )
    session.add(row)
    return row


def _reject_order(
    session: Session,
    portfolio: PaperPortfolio,
    *,
    client_order_id: str,
    mint: str,
    side: str,
    token_id: int | None,
    requested_notional: str | None,
    requested_quantity: str | None,
    reason: str,
    message: str,
) -> PaperOrder:
    order = PaperOrder(
        portfolio_id=portfolio.id,
        client_order_id=client_order_id,
        token_id=token_id,
        mint_address=mint,
        side=side,
        status="REJECTED",
        requested_notional_usd=requested_notional,
        requested_quantity=requested_quantity,
        reject_reason=reason,
        assumptions_json=json.dumps({"detail": message, "proves_tradeability": False}),
    )
    session.add(order)
    session.flush()
    record_decision(
        session,
        portfolio,
        decision_type=f"SIMULATE_{side}",
        outcome="REJECTED",
        reason=reason,
        mint=mint,
        token_id=token_id,
        order_id=order.id,
        details={"detail": message},
    )
    session.commit()
    session.refresh(order)
    return order


def simulate_manual_order(
    session: Session,
    settings: Settings | None = None,
    *,
    mint_address: str,
    side: str,
    quantity_value: str | None = None,
    notional_usd: str | None = None,
    client_order_id: str | None = None,
    now: datetime | None = None,
    extra_assumptions: dict | None = None,
) -> PaperOrder:
    settings = settings or get_settings()
    portfolio = ensure_default_portfolio(session, settings)
    side = side.upper().strip()
    mint = mint_address.strip()
    client_id = (client_order_id or "").strip() or str(uuid.uuid4())
    duplicate = session.scalar(
        select(PaperOrder).where(
            PaperOrder.portfolio_id == portfolio.id,
            PaperOrder.client_order_id == client_id,
        )
    )
    if duplicate is not None:
        return duplicate
    requested_qty = qty_text(quantity_value) if quantity_value not in (None, "") else None
    requested_notional = money_text(notional_usd) if notional_usd not in (None, "") else None

    token = session.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        return _reject_order(
            session,
            portfolio,
            client_order_id=client_id,
            mint=mint,
            side=side,
            token_id=None,
            requested_notional=requested_notional,
            requested_quantity=requested_qty,
            reason="UNKNOWN_TOKEN",
            message="Token is not in the stored catalog. No price is invented.",
        )
    try:
        quote = load_executable_quote(session, token, settings, now=now)
        qty_dec = quantity(quantity_value) if requested_qty else None
        notional_dec = money(notional_usd) if requested_notional else None
        plan = plan_fill(
            side=side,
            quote=quote,
            fee_bps=money(settings.paper_fee_bps),
            slippage_bps=money(settings.paper_slippage_bps),
            requested_quantity=qty_dec,
            requested_notional_usd=notional_dec,
        )
        if extra_assumptions:
            plan.assumptions.update(extra_assumptions)
        _enforce_risk(session, settings, portfolio, token, plan, now=now or utc_now())
        return _commit_fill(
            session,
            settings,
            portfolio,
            token,
            quote.snapshot_id,
            quote.observed_at,
            plan,
            client_id,
            requested_qty,
            requested_notional,
            paper_quote_id=quote.quote_id,
        )
    except QuoteRejected as exc:
        return _reject_order(
            session,
            portfolio,
            client_order_id=client_id,
            mint=mint,
            side=side,
            token_id=token.id,
            requested_notional=requested_notional,
            requested_quantity=requested_qty,
            reason=exc.reason,
            message=exc.message,
        )
    except (SimulationRejected, PaperTradingError) as exc:
        return _reject_order(
            session,
            portfolio,
            client_order_id=client_id,
            mint=mint,
            side=side,
            token_id=token.id,
            requested_notional=requested_notional,
            requested_quantity=requested_qty,
            reason=exc.reason,
            message=str(exc.message if hasattr(exc, "message") else exc),
        )


def _enforce_risk(
    session: Session,
    settings: Settings,
    portfolio: PaperPortfolio,
    token: Token,
    plan: FillPlan,
    *,
    now: datetime,
) -> None:
    cash = money(portfolio.cash_available_usd)
    reserved = money(portfolio.cash_reserved_usd)
    starting = money(portfolio.starting_cash_usd)
    positions = _positions(session, portfolio.id)
    open_rows = _open_positions(positions)
    existing = next((row for row in positions if row.mint_address == token.token_address), None)
    held = quantity(existing.quantity) if existing is not None else ZERO

    if plan.side == SIDE_BUY:
        cost = money(-plan.cash_delta_usd)
        if cost > cash:
            raise PaperTradingError("INSUFFICIENT_CASH", "Order exceeds available cash.")
        if existing is None or held <= ZERO:
            if len(open_rows) >= int(settings.paper_max_open_positions):
                raise PaperTradingError("MAX_OPEN_POSITIONS", "Maximum simultaneous open positions reached.")
        new_cost = (money(existing.cost_basis_usd) if existing else ZERO) + plan.fill_notional_usd
        max_alloc = money(starting * money(settings.paper_max_allocation_pct))
        if new_cost > max_alloc:
            raise PaperTradingError("MAX_ALLOCATION", "Order exceeds maximum allocation per token.")
        invested = sum((money(row.cost_basis_usd) for row in open_rows), ZERO)
        if existing is None or held <= ZERO:
            invested += plan.fill_notional_usd
        else:
            invested = invested - money(existing.cost_basis_usd) + new_cost
        max_exposure = money(starting * money(settings.paper_max_exposure_pct))
        if invested > max_exposure:
            raise PaperTradingError("MAX_EXPOSURE", "Order exceeds maximum total portfolio exposure.")
    else:
        if held < plan.quantity:
            raise PaperTradingError("INSUFFICIENT_HOLDINGS", "Sell quantity exceeds holdings.")
        loss = _daily_realized_loss(session, portfolio.id, now)
        projected = ZERO
        if existing is not None and held > ZERO:
            avg = money(existing.average_cost_usd)
            proceeds = plan.cash_delta_usd
            cost_out = money(plan.quantity * avg)
            projected = money(proceeds - cost_out)
        max_loss = money(settings.paper_max_daily_realized_loss_usd)
        if projected < ZERO and money(loss + projected) < -max_loss:
            raise PaperTradingError("MAX_DAILY_LOSS", "Sell would exceed maximum daily realized loss.")
    if reserved < ZERO:
        raise PaperTradingError("INVALID_RESERVED", "Reserved cash cannot be negative.")


def _commit_fill(
    session: Session,
    settings: Settings,
    portfolio: PaperPortfolio,
    token: Token,
    snapshot_id: int | None,
    observed_at: datetime,
    plan: FillPlan,
    client_order_id: str,
    requested_qty: str | None,
    requested_notional: str | None,
    paper_quote_id: int | None = None,
) -> PaperOrder:
    now = utc_now()
    try:
        order = PaperOrder(
            portfolio_id=portfolio.id,
            client_order_id=client_order_id,
            token_id=token.id,
            mint_address=token.token_address,
            side=plan.side,
            status="FILLED",
            requested_notional_usd=requested_notional,
            requested_quantity=requested_qty,
            quote_snapshot_id=snapshot_id,
            paper_quote_id=paper_quote_id,
            observed_price_usd=money_text(plan.observed_price_usd),
            observed_at=observed_at,
            simulated_price_usd=money_text(plan.simulated_price_usd),
            fill_quantity=qty_text(plan.quantity),
            fill_notional_usd=money_text(plan.fill_notional_usd),
            fee_usd=money_text(plan.fee_usd),
            slippage_usd=money_text(plan.slippage_usd),
            fee_bps=money_text(plan.fee_bps),
            slippage_bps=money_text(plan.slippage_bps),
            assumptions_json=json.dumps(plan.assumptions),
            created_at=now,
            filled_at=now,
        )
        session.add(order)
        session.flush()

        position = session.scalar(
            select(PaperPosition).where(
                PaperPosition.portfolio_id == portfolio.id,
                PaperPosition.mint_address == token.token_address,
            )
        )
        realized = ZERO
        if position is None:
            position = PaperPosition(
                portfolio_id=portfolio.id,
                token_id=token.id,
                mint_address=token.token_address,
                quantity=qty_text(ZERO),
                average_cost_usd=money_text(ZERO),
                cost_basis_usd=money_text(ZERO),
                realized_pnl_usd=money_text(ZERO),
                opened_at=now,
                updated_at=now,
            )
            session.add(position)
            session.flush()

        held = quantity(position.quantity)
        cost_basis = money(position.cost_basis_usd)
        if plan.side == SIDE_BUY:
            new_qty = quantity(held + plan.quantity)
            new_cost = money(cost_basis + plan.fill_notional_usd)
            avg = money(new_cost / new_qty) if new_qty > ZERO else ZERO
            position.quantity = qty_text(new_qty)
            position.cost_basis_usd = money_text(new_cost)
            position.average_cost_usd = money_text(avg)
            cash = money(money(portfolio.cash_available_usd) + plan.cash_delta_usd)
            if cash < ZERO:
                raise PaperTradingError("INSUFFICIENT_CASH", "Cash would be negative.")
            portfolio.cash_available_usd = money_text(cash)
        else:
            if held < plan.quantity:
                raise PaperTradingError("INSUFFICIENT_HOLDINGS", "Sell quantity exceeds holdings.")
            avg = money(position.average_cost_usd)
            cost_out = money(plan.quantity * avg)
            realized = money(plan.cash_delta_usd - cost_out)
            new_qty = quantity(held - plan.quantity)
            if new_qty <= ZERO:
                position.quantity = qty_text(ZERO)
                position.cost_basis_usd = money_text(ZERO)
                position.average_cost_usd = money_text(ZERO)
            else:
                position.quantity = qty_text(new_qty)
                position.cost_basis_usd = money_text(cost_basis - cost_out)
            position.realized_pnl_usd = money_text(money(position.realized_pnl_usd) + realized)
            portfolio.cash_available_usd = money_text(money(portfolio.cash_available_usd) + plan.cash_delta_usd)
            portfolio.realized_pnl_usd = money_text(money(portfolio.realized_pnl_usd) + realized)

        position.token_id = token.id
        position.updated_at = now
        portfolio.fees_paid_usd = money_text(money(portfolio.fees_paid_usd) + plan.fee_usd)
        portfolio.slippage_paid_usd = money_text(money(portfolio.slippage_paid_usd) + plan.slippage_usd)
        portfolio.updated_at = now

        session.add(
            PaperTrade(
                portfolio_id=portfolio.id,
                order_id=order.id,
                position_id=position.id,
                token_id=token.id,
                mint_address=token.token_address,
                side=plan.side,
                quantity=qty_text(plan.quantity),
                observed_price_usd=money_text(plan.observed_price_usd),
                simulated_price_usd=money_text(plan.simulated_price_usd),
                notional_usd=money_text(plan.fill_notional_usd),
                fee_usd=money_text(plan.fee_usd),
                slippage_usd=money_text(plan.slippage_usd),
                realized_pnl_usd=money_text(realized),
                quote_snapshot_id=snapshot_id,
                created_at=now,
            )
        )
        record_decision(
            session,
            portfolio,
            decision_type=f"SIMULATE_{plan.side}",
            outcome="FILLED",
            reason="SIMULATED_FILL",
            mint=token.token_address,
            token_id=token.id,
            order_id=order.id,
            details=plan.assumptions,
        )
        _record_equity(session, portfolio, settings=settings, now=now)
        session.commit()
        session.refresh(order)
        return order
    except Exception:
        session.rollback()
        raise


def _record_equity(
    session: Session,
    portfolio: PaperPortfolio,
    settings: Settings,
    now: datetime,
) -> PaperEquitySnapshot:
    view = build_portfolio_view(session, portfolio, settings, now=now)
    snap = PaperEquitySnapshot(
        portfolio_id=portfolio.id,
        cash_available_usd=money_text(view.cash_available),
        cash_reserved_usd=money_text(view.cash_reserved),
        positions_market_value_usd=money_text(view.positions_market_value) if view.positions_market_value is not None else None,
        unrealized_pnl_usd=money_text(view.unrealized_pnl) if view.unrealized_pnl is not None else None,
        realized_pnl_usd=money_text(view.realized_pnl),
        equity_usd=money_text(view.equity) if view.equity is not None else None,
        valuation_status=view.valuation_status,
        assumptions_json=json.dumps({"unknown_positions": view.unknown_positions}),
        recorded_at=now,
    )
    session.add(snap)
    return snap


def build_portfolio_view(
    session: Session,
    portfolio: PaperPortfolio,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> PortfolioView:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    cash = money(portfolio.cash_available_usd)
    reserved = money(portfolio.cash_reserved_usd)
    realized = money(portfolio.realized_pnl_usd)
    rows = _positions(session, portfolio.id)
    details: list[dict] = []
    market_value = ZERO
    unrealized = ZERO
    unknown: list[str] = []
    known = True
    for row in rows:
        qty = quantity(row.quantity)
        cost = money(row.cost_basis_usd)
        mark = None
        u_pnl = None
        value = None
        status = "FLAT" if qty <= ZERO else "UNKNOWN"
        if qty > ZERO:
            token = session.get(Token, row.token_id) if row.token_id else None
            if token is None:
                token = session.scalar(select(Token).where(Token.token_address == row.mint_address))
            try:
                if token is None:
                    raise QuoteRejected("UNKNOWN_TOKEN", "Token missing for open position.")
                quote = load_executable_quote(session, token, settings, now=now)
                mark = quote.price_usd
                value = money(qty * mark)
                u_pnl = money(value - cost)
                market_value += value
                unrealized += u_pnl
                status = "MARKED"
            except QuoteRejected:
                known = False
                unknown.append(row.mint_address)
                status = "UNKNOWN"
        details.append(
            {
                "id": row.id,
                "mint_address": row.mint_address,
                "token_id": row.token_id,
                "quantity": qty_text(qty),
                "average_cost_usd": money_text(row.average_cost_usd),
                "cost_basis_usd": money_text(cost),
                "realized_pnl_usd": money_text(row.realized_pnl_usd),
                "mark_price_usd": money_text(mark) if mark is not None else None,
                "market_value_usd": money_text(value) if value is not None else None,
                "unrealized_pnl_usd": money_text(u_pnl) if u_pnl is not None else None,
                "valuation_status": status,
                "updated_at": row.updated_at,
            }
        )
    if known:
        equity = money(cash + reserved + market_value)
        valuation = "KNOWN"
        pos_value: Decimal | None = market_value
        u_total: Decimal | None = unrealized
    else:
        equity = None
        valuation = "UNKNOWN"
        pos_value = None
        u_total = None
        if all(quantity(row.quantity) <= ZERO for row in rows):
            equity = money(cash + reserved)
            valuation = "KNOWN"
            pos_value = ZERO
            u_total = ZERO
    return PortfolioView(
        portfolio=portfolio,
        positions=details,
        cash_available=cash,
        cash_reserved=reserved,
        realized_pnl=realized,
        unrealized_pnl=u_total,
        positions_market_value=pos_value,
        equity=equity,
        valuation_status=valuation,
        unknown_positions=unknown,
    )
