"""Hypothetical paper fills from a recorded USD quote. No exchange or chain execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.paper_money import ZERO, PaperMoneyError, bps_fraction, money, quantity
from app.services.paper_quotes import PaperQuote

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


class SimulationRejected(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class FillPlan:
    side: str
    quantity: Decimal
    observed_price_usd: Decimal
    simulated_price_usd: Decimal
    fill_notional_usd: Decimal
    fee_usd: Decimal
    slippage_usd: Decimal
    cash_delta_usd: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    assumptions: dict


def simulated_price(side: str, observed: Decimal, slippage_bps: Decimal) -> Decimal:
    slip = bps_fraction(slippage_bps)
    if side == SIDE_BUY:
        price = money(observed * (Decimal("1") + slip))
    elif side == SIDE_SELL:
        price = money(observed * (Decimal("1") - slip))
    else:
        raise SimulationRejected("INVALID_SIDE", "Side must be BUY or SELL.")
    if price <= ZERO:
        raise SimulationRejected("INVALID_SIMULATED_PRICE", "Simulated execution price must be > 0.")
    return price


def plan_fill(
    *,
    side: str,
    quote: PaperQuote,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    requested_quantity: Decimal | None = None,
    requested_notional_usd: Decimal | None = None,
) -> FillPlan:
    side = side.upper()
    if side not in {SIDE_BUY, SIDE_SELL}:
        raise SimulationRejected("INVALID_SIDE", "Side must be BUY or SELL.")
    if requested_quantity is not None and requested_notional_usd is not None:
        raise SimulationRejected("AMBIGUOUS_SIZE", "Provide quantity or notional_usd, not both.")
    if requested_quantity is None and requested_notional_usd is None:
        raise SimulationRejected("MISSING_SIZE", "Provide quantity or notional_usd.")
    sim_price = simulated_price(side, quote.price_usd, slippage_bps)
    fee_frac = bps_fraction(fee_bps)
    if requested_quantity is not None:
        qty = quantity(requested_quantity)
    elif side == SIDE_BUY:
        cash = money(requested_notional_usd)
        if cash <= ZERO:
            raise SimulationRejected("INVALID_SIZE", "Buy notional must be > 0.")
        qty = quantity(cash / (sim_price * (Decimal("1") + fee_frac)))
    else:
        target = money(requested_notional_usd)
        if target <= ZERO:
            raise SimulationRejected("INVALID_SIZE", "Sell notional must be > 0.")
        qty = quantity(target / sim_price)
    if qty <= ZERO:
        raise SimulationRejected("INVALID_SIZE", "Simulated quantity must be > 0.")
    fill_notional = money(qty * sim_price)
    fee_usd = money(fill_notional * fee_frac)
    slippage_usd = money(abs(sim_price - quote.price_usd) * qty)
    if side == SIDE_BUY:
        cash_delta = money(-(fill_notional + fee_usd))
    else:
        cash_delta = money(fill_notional - fee_usd)
    return FillPlan(
        side=side,
        quantity=qty,
        observed_price_usd=quote.price_usd,
        simulated_price_usd=sim_price,
        fill_notional_usd=fill_notional,
        fee_usd=fee_usd,
        slippage_usd=slippage_usd,
        cash_delta_usd=cash_delta,
        fee_bps=money(fee_bps),
        slippage_bps=money(slippage_bps),
        assumptions={
            "execution": "hypothetical_paper_fill",
            "proves_tradeability": False,
            "blockchain": False,
            "wallet": False,
            "observed_price_field": quote.source,
            "market_cap_used": False,
            "fdv_used": False,
            "quote_snapshot_id": quote.snapshot_id,
            "paper_quote_id": quote.quote_id,
            "quote_freshness_basis": quote.freshness_basis,
            "quote_pair_address": quote.pair_address,
            "quote_observed_at": quote.observed_at.isoformat(),
            "quote_age_seconds": quote.age_seconds,
            "liquidity_usd": format(quote.liquidity_usd, "f") if quote.liquidity_usd is not None else None,
            "buy_price": "observed * (1 + slippage_bps/10000)",
            "sell_price": "observed * (1 - slippage_bps/10000)",
            "fee": "fill_notional * fee_bps/10000",
            "note": "Simulated fill does not prove the order could execute on chain.",
        },
    )


def require_positive_money(value: object, reason: str, message: str) -> Decimal:
    try:
        amount = money(value)
    except PaperMoneyError as exc:
        raise SimulationRejected(reason, str(exc)) from exc
    if amount <= ZERO:
        raise SimulationRejected(reason, message)
    return amount
