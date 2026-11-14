"""Phase 10C.1 automatic paper entry. No wallets, DexScreener refresh, or live trades."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.models import (
    PaperAutoEntryAttempt,
    PaperAutoEntryCycle,
    PaperAutoEntryLock,
    PaperMarketQuote,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    Token,
)
from app.services.paper_auto_entry import (
    STATUS_COMPLETED_WITH_REPORTING_ERROR,
    STATUS_FAILED,
    TRIGGER_MANUAL,
    acquire_entry_lock,
    reconcile_failed_cycle_with_fills,
    release_entry_lock,
    run_auto_entry_cycle,
)
from app.services.paper_entry_scheduler import PaperEntryScheduler, get_entry_scheduler
from app.services.paper_trading import ensure_default_portfolio
from app.timeutil import utc_now
from tests.test_paper_decisions import (
    MINT_BUY,
    MINT_REJECT,
    MINT_WAIT,
    PAIR,
    _buy_ready,
    _forbid_providers,
    _opportunity,
    _quote,
    _security,
    _snapshots,
    _token,
    _wipe,
)


def test_automation_disabled_by_default(client: TestClient) -> None:
    settings = get_settings()
    assert settings.paper_auto_entry_enabled is False
    assert Settings().paper_auto_entry_enabled is False
    status = client.get("/api/v1/paper/auto-entry/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["catchup_pending"] is False
    assert body["automatic_selling"] is False
    assert body["live_trading"] is False
    assert body["wallet_connected"] is False
    assert "disabled by default" in body["note"].lower()


def test_qualified_automatic_paper_entry(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    monkeypatch.setattr(
        "app.services.paper_quote_service.refresh_paper_quote",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("quote refresh must stay explicit")),
    )
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        portfolio = ensure_default_portfolio(session, get_settings())
        start_cash = portfolio.cash_available_usd
        summary = run_auto_entry_cycle(
            session,
            get_settings(),
            trigger=TRIGGER_MANUAL,
            token_addresses=[MINT_BUY],
        )
        assert summary.status == "completed"
        assert summary.tokens_evaluated == 1
        assert summary.eligible == 1
        assert summary.entered == 1
        assert summary.waiting == 0
        assert summary.rejected == 0
        assert summary.orders_created == 1
        order = session.scalar(select(PaperOrder).where(PaperOrder.status == "FILLED"))
        assert order is not None
        assert order.client_order_id.startswith("auto-buy:")
        assert "hypothetical" in (order.assumptions_json or "")
        assert "market_execution" in (order.assumptions_json or "")
        session.refresh(portfolio)
        assert portfolio.cash_available_usd != start_cash
        notional = float(summary.attempts[0]["notional_usd"])
        assert notional <= 5.01
        assert notional >= 4.0
    finally:
        session.close()


def test_wait_and_reject_never_create_orders(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        wait_token = _token(session, MINT_WAIT, "WAIT")
        snaps = _snapshots(session, wait_token)
        _opportunity(session, wait_token, snaps)
        _quote(session, wait_token)
        reject_token = _token(session, MINT_REJECT, "REJ")
        reject_snaps = _snapshots(session, reject_token)
        _opportunity(session, reject_token, reject_snaps)
        _security(
            session,
            reject_token,
            observed_risk=90.0,
            observed_risk_band="critical",
            risk_score=90.0,
            risk_band="critical",
        )
        _quote(session, reject_token)
        summary = run_auto_entry_cycle(
            session,
            get_settings(),
            token_addresses=[MINT_WAIT, MINT_REJECT],
        )
        assert summary.entered == 0
        assert summary.waiting == 1
        assert summary.rejected == 1
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_stale_unproven_and_missing_liquidity_do_not_enter(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(session, token, market_observed_at=utc_now() - timedelta(seconds=120))
        stale = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert stale.entered == 0
        session.execute(delete(PaperMarketQuote))
        session.execute(delete(PaperAutoEntryAttempt))
        session.execute(delete(PaperAutoEntryCycle))
        session.commit()
        _quote(session, token, freshness_basis="RETRIEVAL_UNPROVEN", market_observed_at=None)
        unproven = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert unproven.entered == 0
        session.execute(delete(PaperMarketQuote))
        session.execute(delete(PaperAutoEntryAttempt))
        session.execute(delete(PaperAutoEntryCycle))
        session.commit()
        _quote(session, token, status="REJECTED", reject_reason="MISSING_LIQUIDITY", liquidity_usd=None)
        missing = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert missing.entered == 0
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_unreliable_security_and_low_confidence_skip_orders(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session, MINT_BUY, "LOW")
        snaps = _snapshots(session, token)
        _opportunity(session, token, snaps, status="LOW_CONFIDENCE", confidence=0.2, ranking_label="HIGH_OPPORTUNITY")
        _security(session, token, completeness=0.2, confidence=0.2, status="INSUFFICIENT_DATA")
        _quote(session, token)
        summary = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert summary.entered == 0
        assert summary.waiting >= 1
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_pair_mismatch_does_not_enter(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(session, token, status="REJECTED", reject_reason="PAIR_MISMATCH", pair_address="other")
        summary = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert summary.entered == 0
        assert summary.rejected == 1
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_portfolio_exposure_and_unknown_equity(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        portfolio = ensure_default_portfolio(session, get_settings())
        portfolio.cash_available_usd = "0"
        session.commit()
        cash = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert cash.entered == 0
        portfolio.cash_available_usd = "100"
        session.commit()
        other = Token(token_address="Unk111111111111111111111111111111111111111", symbol="U", chain="solana")
        session.add(other)
        session.flush()
        session.add(
            PaperPosition(
                portfolio_id=portfolio.id,
                token_id=other.id,
                mint_address=other.token_address,
                quantity="1",
                average_cost_usd="1",
                cost_basis_usd="1",
                realized_pnl_usd="0",
            )
        )
        session.commit()
        unknown = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert unknown.entered == 0
        assert any(item["reason"] == "UNKNOWN_EQUITY" for item in unknown.attempts)
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_duplicate_prevention_and_restart_lock(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        first = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert first.entered == 1
        second = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert second.entered == 0
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.status == "FILLED")) == 1
        reasons = {item["reason"] for item in second.attempts}
        assert reasons & {
            "DUPLICATE_OPEN_POSITION",
            "MAX_ALLOCATION",
            "MAX_EXPOSURE",
            "DUPLICATE_CLIENT_ORDER",
            "PENDING_ENTRY",
        }
        stale = PaperAutoEntryCycle(
            trigger="SCHEDULED",
            status="running",
            started_at=utc_now() - timedelta(minutes=10),
            holder="dead-process",
            note="stale",
        )
        session.add(stale)
        session.commit()
        lock = session.get(PaperAutoEntryLock, 1)
        assert lock is not None
        lock.holder = "dead-process"
        lock.cycle_id = stale.id
        lock.lease_until = utc_now() - timedelta(seconds=5)
        session.commit()
        third = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        session.refresh(stale)
        assert stale.status == "interrupted"
        assert third.status == "completed"
        assert third.entered == 0
    finally:
        session.close()


def test_concurrent_cycles_fail_closed(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        held = acquire_entry_lock(session, get_settings(), "tester", utc_now())
        assert held is True
        summary = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert summary.status == "skipped"
        assert summary.cycle_id is None
        assert summary.entered == 0
        release_entry_lock(session, "tester")
    finally:
        session.close()


def test_transaction_rollback_does_not_fill(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        portfolio = ensure_default_portfolio(session, get_settings())
        cash = portfolio.cash_available_usd

        def boom(*_a, **_k):
            raise RuntimeError("forced failure")

        monkeypatch.setattr("app.services.paper_auto_entry.simulate_manual_order", boom)
        summary = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert summary.entered == 0
        assert summary.failed == 1
        session.refresh(portfolio)
        assert portfolio.cash_available_usd == cash
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_scheduler_cancellation_and_no_catchup() -> None:
    async def _run() -> None:
        scheduler = PaperEntryScheduler()
        settings = get_settings().model_copy(
            update={"paper_auto_entry_enabled": True, "paper_auto_entry_interval_minutes": 30}
        )
        await scheduler.start(settings)
        assert scheduler.running is True
        assert scheduler.next_run_at is not None
        await scheduler.stop()
        assert scheduler.running is False

    import asyncio

    asyncio.run(_run())
    assert get_settings().paper_auto_entry_enabled is False
    assert get_entry_scheduler().enabled is False


def test_api_run_status_history_and_no_external_execution(client: TestClient, monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
    finally:
        session.close()
    run = client.post("/api/v1/paper/auto-entry/run", json={"token_addresses": [MINT_BUY]})
    assert run.status_code == 200
    body = run.json()
    assert body["entered"] == 1
    assert body["market_execution"] is False
    assert body["automatic_selling"] is False
    status = client.get("/api/v1/paper/auto-entry/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    history = client.get("/api/v1/paper/auto-entry/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["entered"] == 1
    paper_status = client.get("/api/v1/paper/status").json()
    assert paper_status["live_trading"] is False
    assert paper_status["automatic_orders"] is False
    assert paper_status["wallet_connected"] is False


def test_reporting_failure_after_fill_does_not_rollback_or_duplicate(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        original = __import__("app.services.paper_auto_entry", fromlist=["_dump_json"])._dump_json

        def boom(value):
            raise TypeError("Object of type datetime is not JSON serializable")

        monkeypatch.setattr("app.services.paper_auto_entry._dump_json", boom)
        summary = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert summary.entered == 1
        assert summary.orders_created == 1
        assert summary.status == STATUS_COMPLETED_WITH_REPORTING_ERROR
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.status == "FILLED")) == 1
        position = session.scalar(select(PaperPosition).where(PaperPosition.mint_address == MINT_BUY))
        assert position is not None
        qty = position.quantity
        cash = session.scalar(select(PaperPortfolio)).cash_available_usd
        monkeypatch.setattr("app.services.paper_auto_entry._dump_json", original)
        second = run_auto_entry_cycle(session, get_settings(), token_addresses=[MINT_BUY])
        assert second.entered == 0
        assert session.scalar(select(func.count()).select_from(PaperOrder).where(PaperOrder.status == "FILLED")) == 1
        session.refresh(position)
        assert position.quantity == qty
        assert session.scalar(select(PaperPortfolio)).cash_available_usd == cash
    finally:
        session.close()


def test_reconcile_failed_cycle_with_fills_does_not_touch_book() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        portfolio = ensure_default_portfolio(session, get_settings())
        order = PaperOrder(
            portfolio_id=portfolio.id,
            client_order_id="auto-buy:1:dead:1",
            mint_address=MINT_BUY,
            side="BUY",
            status="FILLED",
        )
        session.add(order)
        cycle = PaperAutoEntryCycle(
            trigger="MANUAL",
            status=STATUS_FAILED,
            entered=1,
            orders_created=1,
            errors_json='[{"detail": "Object of type datetime is not JSON serializable"}]',
            note="inconsistent",
        )
        session.add(cycle)
        session.commit()
        report = reconcile_failed_cycle_with_fills(session, cycle.id)
        assert report["repaired"] is True
        session.refresh(cycle)
        assert cycle.status == STATUS_COMPLETED_WITH_REPORTING_ERROR
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 1
        assert session.scalar(select(PaperOrder)).status == "FILLED"
    finally:
        session.close()
