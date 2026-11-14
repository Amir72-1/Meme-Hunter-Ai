"""Phase 10A paper portfolio, simulator, and accounting tests. No live trading."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    MarketSnapshot,
    PaperDecision,
    PaperEquitySnapshot,
    PaperAutoEntryAttempt,
    PaperAutoEntryCycle,
    PaperAutoExitAttempt,
    PaperAutoExitCycle,
    PaperMarketQuote,
    PaperOpportunityDecision,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperPositionObservation,
    PaperTrade,
    Token,
)
from app.services.paper_money import money, quantity
from app.services.paper_quotes import QuoteRejected, load_executable_quote
from app.services.paper_simulator import plan_fill, simulated_price
from app.services.paper_trading import (
    build_portfolio_view,
    ensure_default_portfolio,
    simulate_manual_order,
)
from app.timeutil import utc_now
from tests.test_security import MINT, TOKEN_A

MINT_B = "MintB22222222222222222222222222222222222222"


def _wipe(session) -> None:
    session.execute(delete(PaperAutoExitAttempt))
    session.execute(delete(PaperAutoExitCycle))
    session.execute(delete(PaperPositionObservation))
    session.execute(delete(PaperAutoEntryAttempt))
    session.execute(delete(PaperAutoEntryCycle))
    session.execute(delete(PaperOpportunityDecision))
    session.execute(delete(PaperTrade))
    session.execute(delete(PaperOrder))
    session.execute(delete(PaperDecision))
    session.execute(delete(PaperEquitySnapshot))
    session.execute(delete(PaperPosition))
    session.execute(delete(PaperPortfolio))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(PaperMarketQuote))
    session.execute(delete(Token))
    session.commit()


def _loose_limits(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_max_allocation_pct", 1.0)
    monkeypatch.setattr(settings, "paper_max_exposure_pct", 1.0)
    monkeypatch.setattr(settings, "paper_max_daily_realized_loss_usd", 100.0)
    monkeypatch.setattr(settings, "paper_max_open_positions", 10)


def _token(session, mint: str = MINT, symbol: str = "A") -> Token:
    row = Token(token_address=mint, symbol=symbol, name=symbol, chain="solana")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _quote(session, token: Token, *, price: float = 1.0, liquidity: float | None = 25000, age_seconds: float = 1) -> MarketSnapshot:
    snap = MarketSnapshot(
        token_id=token.id,
        price_usd=price,
        liquidity_usd=liquidity,
        recorded_at=utc_now() - timedelta(seconds=age_seconds),
        discovery_source="test",
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)
    return snap


def test_fee_and_slippage_math_distinguishes_observed_and_simulated() -> None:
    from app.services.paper_quotes import PaperQuote

    quote = PaperQuote(
        snapshot_id=1,
        token_id=1,
        mint_address=MINT,
        price_usd=money("1"),
        observed_at=utc_now(),
        age_seconds=1,
        liquidity_usd=money("25000"),
    )
    buy = plan_fill(
        side="BUY",
        quote=quote,
        fee_bps=money("30"),
        slippage_bps=money("50"),
        requested_quantity=money("10"),
    )
    assert buy.observed_price_usd == money("1")
    assert buy.simulated_price_usd == money("1.005")
    assert buy.fill_notional_usd == money("10.05")
    assert buy.fee_usd == money("0.03015")
    assert buy.cash_delta_usd == money("-10.08015")
    assert buy.assumptions["proves_tradeability"] is False
    assert buy.assumptions["market_cap_used"] is False
    sell = plan_fill(
        side="SELL",
        quote=quote,
        fee_bps=money("30"),
        slippage_bps=money("50"),
        requested_quantity=money("10"),
    )
    assert sell.simulated_price_usd == money("0.995")
    assert simulated_price("BUY", money("1"), money("50")) == money("1.005")


def test_idempotent_initialization_does_not_reset() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        first = ensure_default_portfolio(session, get_settings())
        first.cash_available_usd = "90.00000000"
        session.commit()
        second = ensure_default_portfolio(session, get_settings())
        assert first.id == second.id
        assert second.cash_available_usd == "90.00000000"
        assert money(second.starting_cash_usd) == money("100")
        rows = list(session.scalars(select(PaperPortfolio)).all())
        assert len(rows) == 1
    finally:
        session.close()


def test_buy_sell_partial_close_and_accounting(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token, price=1.0)
        buy = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="10",
            client_order_id="buy-1",
        )
        assert buy.status == "FILLED"
        portfolio = session.scalar(select(PaperPortfolio).where(PaperPortfolio.name == "default"))
        assert money(portfolio.cash_available_usd) == money("89.91985")
        position = session.scalar(select(PaperPosition))
        assert quantity(position.quantity) == quantity("10")
        sell = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="SELL",
            quantity_value="4",
            client_order_id="sell-partial",
        )
        assert sell.status == "FILLED"
        session.refresh(position)
        assert quantity(position.quantity) == quantity("6")
        assert money(position.cost_basis_usd) == money(quantity("6") * money(position.average_cost_usd))
        trades = list(session.scalars(select(PaperTrade).order_by(PaperTrade.id)).all())
        assert len(trades) == 2
        assert trades[0].observed_price_usd != trades[0].simulated_price_usd
    finally:
        session.close()


def test_insufficient_cash_and_holdings(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token, price=50.0)
        order = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="10",
            client_order_id="cash-fail",
        )
        assert order.status == "REJECTED"
        assert order.reject_reason == "INSUFFICIENT_CASH"
        _quote(session, token, price=1.0)
        simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="small-buy",
        )
        denied = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="SELL",
            quantity_value="5",
            client_order_id="over-sell",
        )
        assert denied.status == "REJECTED"
        assert denied.reject_reason == "INSUFFICIENT_HOLDINGS"
    finally:
        session.close()


def test_duplicate_order_is_idempotent(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token)
        first = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="dup-1",
        )
        second = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="dup-1",
        )
        assert first.id == second.id
        count = len(list(session.scalars(select(PaperOrder)).all()))
        assert count == 1
        trades = list(session.scalars(select(PaperTrade)).all())
        assert len(trades) == 1
    finally:
        session.close()


def test_stale_missing_and_invalid_quotes() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        stale = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="no-snap",
        )
        assert stale.reject_reason == "MISSING_QUOTE"
        _quote(session, token, price=1.0, age_seconds=120)
        aged = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="stale",
        )
        assert aged.reject_reason == "STALE_QUOTE"
        token_b = _token(session, mint=MINT_B, symbol="B")
        _quote(session, token_b, price=1.0, liquidity=None)
        missing_liq = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT_B,
            side="BUY",
            quantity_value="1",
            client_order_id="no-liq",
        )
        assert missing_liq.reject_reason == "MISSING_LIQUIDITY"
        token_c = _token(session, mint=TOKEN_A, symbol="C")
        _quote(session, token_c, price=0.0)
        zero = simulate_manual_order(
            session,
            get_settings(),
            mint_address=TOKEN_A,
            side="BUY",
            quantity_value="1",
            client_order_id="zero",
        )
        assert zero.reject_reason == "INVALID_PRICE"
        token_d = _token(session, mint="MintD22222222222222222222222222222222222222", symbol="D")
        snap = MarketSnapshot(token_id=token_d.id, price_usd=None, fdv=1000, market_cap=1000, liquidity_usd=25000, recorded_at=utc_now())
        session.add(snap)
        session.commit()
        fdv = simulate_manual_order(
            session,
            get_settings(),
            mint_address=token_d.token_address,
            side="BUY",
            quantity_value="1",
            client_order_id="fdv",
        )
        assert fdv.reject_reason == "MISSING_PRICE"
    finally:
        session.close()


def test_missing_liquidity_is_not_zero() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        _quote(session, token, liquidity=None)
        with pytest.raises(QuoteRejected) as caught:
            load_executable_quote(session, token, get_settings())
        assert caught.value.reason == "MISSING_LIQUIDITY"
    finally:
        session.close()


def test_low_liquidity_rejected() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        _quote(session, token, liquidity=1.0)
        order = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="thin",
        )
        assert order.reject_reason == "INSUFFICIENT_LIQUIDITY"
    finally:
        session.close()


def test_unknown_valuation_when_quote_stale(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token, price=1.0, age_seconds=1)
        simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="mark-buy",
        )
        snap = session.scalar(select(MarketSnapshot).order_by(MarketSnapshot.id.desc()))
        snap.recorded_at = utc_now() - timedelta(seconds=120)
        session.commit()
        portfolio = session.scalar(select(PaperPortfolio))
        view = build_portfolio_view(session, portfolio, get_settings())
        assert view.valuation_status == "UNKNOWN"
        assert view.equity is None
        assert view.unrealized_pnl is None
        assert money(view.cash_available) > 0
    finally:
        session.close()


def test_restart_persistence(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token)
        simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="persist",
        )
        cash = session.scalar(select(PaperPortfolio)).cash_available_usd
        session.close()
        session = get_session_factory()()
        again = ensure_default_portfolio(session, get_settings())
        assert again.cash_available_usd == cash
        assert session.scalar(select(PaperTrade)).quantity is not None
    finally:
        session.close()


def test_transaction_rollback_on_failure(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token)
        ensure_default_portfolio(session, get_settings())
        start_cash = session.scalar(select(PaperPortfolio)).cash_available_usd

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced failure")

        monkeypatch.setattr("app.services.paper_trading._record_equity", boom)
        with pytest.raises(RuntimeError):
            simulate_manual_order(
                session,
                get_settings(),
                mint_address=MINT,
                side="BUY",
                quantity_value="1",
                client_order_id="rollback",
            )
        session2 = get_session_factory()()
        try:
            portfolio = session2.scalar(select(PaperPortfolio))
            assert portfolio.cash_available_usd == start_cash
            assert list(session2.scalars(select(PaperTrade)).all()) == []
        finally:
            session2.close()
    finally:
        session.close()


def test_no_chain_or_wallet_in_assumptions(monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token)
        order = simulate_manual_order(
            session,
            get_settings(),
            mint_address=MINT,
            side="BUY",
            notional_usd="5",
            client_order_id="assumptions",
        )
        import json

        payload = json.loads(order.assumptions_json)
        assert payload["blockchain"] is False
        assert payload["wallet"] is False
        assert payload["proves_tradeability"] is False
        assert "private" not in (order.assumptions_json or "").lower()
        assert "seed" not in (order.assumptions_json or "").lower()
    finally:
        session.close()


def test_paper_api_status_and_manual_simulate(client: TestClient, monkeypatch) -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _loose_limits(monkeypatch)
        token = _token(session)
        _quote(session, token)
    finally:
        session.close()
    status = client.get("/api/v1/paper/status")
    assert status.status_code == 200
    body = status.json()
    assert body["mode"] == "PAPER_ONLY"
    assert body["live_trading"] is False
    assert body["automatic_orders"] is False
    assert body["wallet_connected"] is False
    assert body["limits"]["max_quote_age_seconds"] == 60
    portfolio = client.get("/api/v1/paper/portfolio")
    assert portfolio.status_code == 200
    assert portfolio.json()["starting_cash_usd"].startswith("100")
    filled = client.post(
        "/api/v1/paper/simulate",
        json={"token_address": MINT, "side": "BUY", "quantity": "1", "client_order_id": "api-buy"},
    )
    assert filled.status_code == 200
    assert filled.json()["status"] == "FILLED"
    dup = client.post(
        "/api/v1/paper/simulate",
        json={"token_address": MINT, "side": "BUY", "quantity": "1", "client_order_id": "api-buy"},
    )
    assert dup.json()["id"] == filled.json()["id"]
    assert client.get("/api/v1/paper/orders").json()["items"]
    assert client.get("/api/v1/paper/trades").json()["items"]
    assert client.get("/api/v1/paper/positions").json()["items"]
    assert client.get("/api/v1/paper/decisions").json()["items"]
