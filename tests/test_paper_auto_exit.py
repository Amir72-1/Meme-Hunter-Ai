"""Phase 10C.2 automatic paper exits. No wallets, DexScreener refresh, or live trades."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.models import (
    PaperAutoEntryLock,
    PaperAutoExitCycle,
    PaperMarketQuote,
    PaperOrder,
    PaperPosition,
    PaperPositionObservation,
    Token,
)
from app.services.paper_auto_entry import acquire_entry_lock, release_entry_lock, run_auto_entry_cycle
from app.services.paper_auto_exit import (
    EXIT_PENDING_DATA,
    TRIGGER_MANUAL,
    evaluate_exit,
    run_auto_exit_cycle,
)
from app.services.paper_book_lock import acquire_book_lock, release_book_lock
from app.services.paper_exit_scheduler import PaperExitScheduler, get_exit_scheduler
from app.services.paper_money import quantity
from app.services.paper_trading import ensure_default_portfolio, simulate_manual_order
from app.timeutil import utc_now
from tests.test_paper_decisions import (
    MINT_BUY,
    PAIR,
    _forbid_providers,
    _quote,
    _security,
    _token,
    _wipe,
)

PAIR_OTHER = "So11111111111111111111111111111111111111112"


def _open_long(session, mint: str = MINT_BUY, price: float = 1.0, notional: str = "5"):
    token = _token(session, mint, "EX")
    _quote(session, token, price_usd=price)
    order = simulate_manual_order(
        session,
        get_settings(),
        mint_address=mint,
        side="BUY",
        notional_usd=notional,
    )
    assert order.status == "FILLED"
    position = session.scalar(select(PaperPosition).where(PaperPosition.mint_address == mint))
    assert position is not None
    assert quantity(position.quantity) > 0
    return token, position


def _latest_quote(session, token: Token, **overrides) -> PaperMarketQuote:
    now = utc_now()
    payload = {
        "token_id": token.id,
        "mint_address": token.token_address,
        "pair_address": token.pair_address or PAIR,
        "price_usd": 1.0,
        "liquidity_usd": 25000.0,
        "source": "dexscreener_on_demand",
        "retrieved_at": now,
        "market_observed_at": now,
        "freshness_basis": "PROVIDER_TIMESTAMP",
        "status": "ACCEPTED",
        "pair_match_status": "TRACKED_PAIR",
        "cache_hit": 0,
    }
    payload.update(overrides)
    row = PaperMarketQuote(**payload)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_automation_disabled_by_default(client: TestClient) -> None:
    settings = get_settings()
    assert settings.paper_auto_exit_enabled is False
    assert Settings().paper_auto_exit_enabled is False
    status = client.get("/api/v1/paper/auto-exit/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["catchup_pending"] is False
    assert body["live_trading"] is False
    assert body["wallet_connected"] is False
    assert "disabled by default" in body["note"].lower()


def test_stop_loss_exits_remaining_quantity(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    monkeypatch.setattr(
        "app.services.paper_quote_service.refresh_paper_quote",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("quote refresh must stay explicit")),
    )
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        assert monitor.triggered is False
        stop = float(monitor.stop_loss_usd)
        _latest_quote(session, token, price_usd=stop * 0.95)
        summary = run_auto_exit_cycle(session, get_settings(), trigger=TRIGGER_MANUAL)
        assert summary.status == "completed"
        assert summary.exited == 1
        assert summary.orders_created == 1
        assert summary.attempts[0]["reason"] == "STOP_LOSS"
        session.refresh(position)
        assert quantity(position.quantity) == 0
        order = session.scalar(select(PaperOrder).where(PaperOrder.side == "SELL", PaperOrder.status == "FILLED"))
        assert order is not None
        assert order.client_order_id.startswith("auto-sell:")
        assert "hypothetical" in (order.assumptions_json or "")
        assert "market_execution" in (order.assumptions_json or "")
    finally:
        session.close()


def test_take_profit_exit(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        take = float(monitor.take_profit_usd)
        _latest_quote(session, token, price_usd=take * 1.02)
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 1
        assert summary.attempts[0]["reason"] == "TAKE_PROFIT"
    finally:
        session.close()


def test_trailing_stop_uses_observed_high_only(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        future = utc_now() + timedelta(hours=2)
        session.add(
            PaperPositionObservation(
                position_id=position.id,
                portfolio_id=position.portfolio_id,
                token_id=token.id,
                mint_address=position.mint_address,
                observed_at=future,
                retrieved_at=future,
                price_usd=9.0,
                accepted=1,
            )
        )
        session.commit()
        after_open = utc_now()
        _latest_quote(session, token, price_usd=1.20, market_observed_at=after_open, retrieved_at=after_open)
        marked = evaluate_exit(session, position, get_settings(), persist_mark=True)
        assert marked.trailing_active is True
        assert float(marked.high_water_usd) < 8.0
        trail = float(marked.trailing_stop_usd)
        later = utc_now()
        _latest_quote(
            session,
            token,
            price_usd=trail * 0.99,
            market_observed_at=later,
            retrieved_at=later,
        )
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 1
        assert summary.attempts[0]["reason"] == "TRAILING_STOP"
    finally:
        session.close()


def test_max_hold_pending_without_valid_quote(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        position.opened_at = utc_now() - timedelta(hours=25)
        session.commit()
        _latest_quote(
            session,
            token,
            price_usd=1.0,
            market_observed_at=utc_now() - timedelta(seconds=120),
        )
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 0
        assert summary.pending == 1
        assert summary.attempts[0]["outcome"] == EXIT_PENDING_DATA
        session.refresh(position)
        assert quantity(position.quantity) > 0
    finally:
        session.close()


def test_max_hold_fills_with_valid_quote(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        position.opened_at = utc_now() - timedelta(hours=25)
        session.commit()
        _latest_quote(session, token, price_usd=1.0)
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 1
        assert summary.attempts[0]["reason"] == "MAX_HOLDING"
    finally:
        session.close()


def test_security_deterioration_vs_missing_and_unreliable(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        missing = evaluate_exit(session, position, get_settings(), persist_mark=False)
        assert missing.signal != "SECURITY_DETERIORATION"
        _security(
            session,
            token,
            observed_risk=95.0,
            observed_risk_band="critical",
            risk_score=95.0,
            risk_band="critical",
            completeness=0.20,
            confidence=0.20,
            scanned_at=utc_now(),
        )
        unreliable = evaluate_exit(session, position, get_settings(), persist_mark=False)
        assert unreliable.signal != "SECURITY_DETERIORATION"
        _security(
            session,
            token,
            observed_risk=92.0,
            observed_risk_band="high",
            risk_score=92.0,
            risk_band="high",
            completeness=1.0,
            confidence=1.0,
            scanned_at=utc_now(),
        )
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 1
        assert summary.attempts[0]["reason"] == "SECURITY_DETERIORATION"
    finally:
        session.close()


def test_security_overrides_stop_loss(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(session, token, price_usd=float(monitor.stop_loss_usd) * 0.9)
        _security(
            session,
            token,
            observed_risk=99.0,
            observed_risk_band="critical",
            risk_score=99.0,
            risk_band="critical",
            completeness=1.0,
            confidence=1.0,
            scanned_at=utc_now(),
        )
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.attempts[0]["reason"] == "SECURITY_DETERIORATION"
    finally:
        session.close()


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"market_observed_at": None, "freshness_basis": "RETRIEVAL_UNPROVEN"}, "UNPROVEN_QUOTE_FRESHNESS"),
        ({"liquidity_usd": None}, "MISSING_LIQUIDITY"),
        ({"pair_address": PAIR_OTHER}, "PAIR_MISMATCH"),
        ({"status": "REJECTED", "reject_reason": "SUSPICIOUS_DISCONTINUITY", "price_usd": 50.0}, "SUSPICIOUS_DISCONTINUITY"),
        ({"status": "REJECTED", "reject_reason": "PAIR_CHANGED"}, "PAIR_CHANGED"),
    ],
)
def test_invalid_quotes_pending_when_triggered(monkeypatch, overrides, reason) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        position.opened_at = utc_now() - timedelta(hours=30)
        session.commit()
        _latest_quote(session, token, **overrides)
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 0
        assert summary.pending == 1
        assert summary.attempts[0]["outcome"] == EXIT_PENDING_DATA
        assert summary.attempts[0]["reason"] == reason
        session.refresh(position)
        assert quantity(position.quantity) > 0
    finally:
        session.close()


def test_stale_quote_is_not_a_successful_stop(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(
            session,
            token,
            price_usd=float(monitor.stop_loss_usd) * 0.5,
            market_observed_at=utc_now() - timedelta(seconds=180),
        )
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 0
        assert summary.pending == 0
        assert summary.attempts[0]["outcome"] == "WATCH"
        session.refresh(position)
        assert quantity(position.quantity) > 0
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.side == "SELL")) == 0
    finally:
        session.close()


def test_partial_then_remaining_exit(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session, notional="8")
        held = quantity(position.quantity)
        half = held / 2
        sold = simulate_manual_order(
            session,
            get_settings(),
            mint_address=token.token_address,
            side="SELL",
            quantity_value=format(half, "f"),
        )
        assert sold.status == "FILLED"
        session.refresh(position)
        remaining = quantity(position.quantity)
        assert remaining > 0
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(session, token, price_usd=float(monitor.stop_loss_usd) * 0.9)
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 1
        session.refresh(position)
        assert quantity(position.quantity) == 0
        sell = session.scalar(
            select(PaperOrder).where(
                PaperOrder.client_order_id.like("auto-sell:%"),
                PaperOrder.status == "FILLED",
            )
        )
        assert sell is not None
        assert quantity(sell.fill_quantity) == remaining
    finally:
        session.close()


def test_duplicate_sell_is_skipped(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(session, token, price_usd=float(monitor.stop_loss_usd) * 0.9)
        first = run_auto_exit_cycle(session, get_settings())
        assert first.exited == 1
        second = run_auto_exit_cycle(session, get_settings())
        assert second.exited == 0
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.side == "SELL")) == 1
    finally:
        session.close()


def test_entry_and_exit_share_lease(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _open_long(session)
        held = acquire_entry_lock(session, get_settings(), "entry-holder", utc_now())
        assert held is True
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.status == "skipped"
        assert summary.cycle_id is None
        assert summary.exited == 0
        release_entry_lock(session, "entry-holder")
        held_exit = acquire_book_lock(session, get_settings(), "exit-holder", utc_now())
        assert held_exit is True
        entry = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert entry.status == "skipped"
        assert entry.entered == 0
        release_book_lock(session, "exit-holder")
    finally:
        session.close()


def test_stale_exit_cycle_interrupted_on_restart(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _open_long(session)
        stale = PaperAutoExitCycle(
            trigger="SCHEDULED",
            status="running",
            started_at=utc_now() - timedelta(minutes=10),
            holder="dead-process",
            note="stale",
        )
        session.add(stale)
        session.commit()
        lock = session.get(PaperAutoEntryLock, 1)
        if lock is None:
            lock = PaperAutoEntryLock(id=1)
            session.add(lock)
            session.commit()
        lock.holder = "dead-process"
        lock.cycle_id = stale.id
        lock.cycle_kind = "exit"
        lock.lease_until = utc_now() - timedelta(seconds=5)
        session.commit()
        summary = run_auto_exit_cycle(session, get_settings())
        session.refresh(stale)
        assert stale.status == "interrupted"
        assert summary.status == "completed"
    finally:
        session.close()


def test_transaction_rollback_does_not_sell(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        qty = position.quantity
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(session, token, price_usd=float(monitor.stop_loss_usd) * 0.9)

        def boom(*_a, **_k):
            raise RuntimeError("forced failure")

        monkeypatch.setattr("app.services.paper_auto_exit.simulate_manual_order", boom)
        summary = run_auto_exit_cycle(session, get_settings())
        assert summary.exited == 0
        assert summary.failed == 1
        session.refresh(position)
        assert position.quantity == qty
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.side == "SELL")) == 0
    finally:
        session.close()


def test_scheduler_cancellation_and_no_catchup() -> None:
    async def _run() -> None:
        scheduler = PaperExitScheduler()
        settings = get_settings().model_copy(
            update={"paper_auto_exit_enabled": True, "paper_auto_exit_interval_seconds": 120}
        )
        await scheduler.start(settings)
        assert scheduler.running is True
        assert scheduler.next_run_at is not None
        await scheduler.stop()
        assert scheduler.running is False

    import asyncio

    asyncio.run(_run())
    assert get_settings().paper_auto_exit_enabled is False
    assert get_exit_scheduler().enabled is False


def test_api_run_status_history_positions(client: TestClient, monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token, position = _open_long(session)
        monitor = evaluate_exit(session, position, get_settings(), persist_mark=False)
        _latest_quote(session, token, price_usd=float(monitor.stop_loss_usd) * 0.9)
    finally:
        session.close()
    listed = client.get("/api/v1/paper/auto-exit/positions")
    assert listed.status_code == 200
    assert listed.json()["market_execution"] is False
    assert listed.json()["items"]
    run = client.post("/api/v1/paper/auto-exit/run")
    assert run.status_code == 200
    body = run.json()
    assert body["exited"] == 1
    assert body["market_execution"] is False
    assert body["wallet"] is False
    status = client.get("/api/v1/paper/auto-exit/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    history = client.get("/api/v1/paper/auto-exit/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["exited"] == 1
    paper_status = client.get("/api/v1/paper/status").json()
    assert paper_status["live_trading"] is False
    assert paper_status["wallet_connected"] is False
