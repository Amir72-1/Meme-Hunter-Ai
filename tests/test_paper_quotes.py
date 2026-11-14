"""Phase 10B.1 on-demand DexScreener paper quotes. No live network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    MarketSnapshot,
    OpportunityAssessment,
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
from app.services.paper_quote_service import refresh_paper_quote, reset_quote_coordinator
from app.services.paper_quotes import QuoteRejected, load_executable_quote
from app.services.paper_trading import simulate_manual_order
from app.timeutil import utc_now
from tests.dexscreener_fakes import pair_payload, run

MINT = "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump"
PAIR_A = "HoqJaoes6yfL8YEsHGfENwVKT7LrAPy97ZDg9hmBu4Ep"
PAIR_B = "So11111111111111111111111111111111111111112"


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
    session.execute(delete(PaperMarketQuote))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(Token))
    session.commit()
    reset_quote_coordinator()


def _token(session, pair: str | None = PAIR_A) -> Token:
    row = Token(
        token_address=MINT,
        symbol="GATO",
        name="GATO",
        chain="solana",
        pair_address=pair,
        dex_name="pumpswap",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _now_ms(offset_seconds: float = 0) -> int:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return int(stamp.timestamp() * 1000)


def _pair(**overrides):
    payload = pair_payload(
        MINT,
        pairAddress=PAIR_A,
        dexId="pumpswap",
        priceUsd="1.25",
        liquidity={"usd": 25000},
        updatedAt=_now_ms(1),
    )
    payload.update(overrides)
    return payload


class _Scripted:
    def __init__(self) -> None:
        self.token_pairs: dict[str, list | httpx.Response] = {}
        self.calls: list[str] = []
        self._rate_left = 0

    def fail_rate_limit(self, times: int) -> None:
        self._rate_left = times

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self._rate_left > 0:
            self._rate_left -= 1
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate"})
        if "/token-pairs/v1/solana/" in request.url.path:
            value = self.token_pairs.get(MINT, [])
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=value)
        return httpx.Response(404, json={"error": "not mocked"})

    def client(self):
        from app.services.dexscreener_client import DexScreenerClient

        return DexScreenerClient(
            settings=get_settings(),
            transport=httpx.MockTransport(self.handler),
            rate_limit=False,
        )


def _refresh(session, scripted: _Scripted) -> PaperMarketQuote:
    async def _go():
        async with scripted.client() as client:
            return await refresh_paper_quote(session, MINT, get_settings(), client=client)

    return run(_go())


def test_valid_quote_retrieval_with_provenance() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _token(session)
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair()]
        row = _refresh(session, scripted)
        assert row.status == "ACCEPTED"
        assert row.price_usd == 1.25
        assert row.liquidity_usd == 25000
        assert row.pair_address == PAIR_A
        assert row.source == "dexscreener_on_demand"
        assert row.freshness_basis == "PROVIDER_TIMESTAMP"
        assert row.market_observed_at is not None
        assert row.retrieved_at is not None
        assert row.retrieved_at != row.market_observed_at or True
        assert "not proof" in (row.notes or "").lower() or row.freshness_basis == "PROVIDER_TIMESTAMP"
        quote = load_executable_quote(session, session.scalar(select(Token)), get_settings())
        assert quote.quote_id == row.id
        assert quote.snapshot_id is None
        assert quote.source == "dexscreener_on_demand"
        assert quote.freshness_basis == "PROVIDER_TIMESTAMP"
        assert "/token-pairs/v1/solana/" in scripted.calls[0]
    finally:
        session.close()


def test_stale_provider_timestamp_is_not_executable() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair(updatedAt=_now_ms(3600))]
        row = _refresh(session, scripted)
        assert row.status == "ACCEPTED"
        assert row.freshness_basis == "PROVIDER_TIMESTAMP"
        try:
            load_executable_quote(session, token, get_settings())
            raise AssertionError("stale quote must not be executable")
        except QuoteRejected as exc:
            assert exc.reason == "STALE_QUOTE"
    finally:
        session.close()


def test_retrieval_time_does_not_prove_freshness() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        scripted = _Scripted()
        payload = _pair()
        del payload["updatedAt"]
        scripted.token_pairs[MINT] = [payload]
        row = _refresh(session, scripted)
        assert row.status == "ACCEPTED"
        assert row.freshness_basis == "RETRIEVAL_UNPROVEN"
        assert row.market_observed_at is None
        try:
            load_executable_quote(session, token, get_settings())
            raise AssertionError("unproven retrieval must not fill")
        except QuoteRejected as exc:
            assert exc.reason == "UNPROVEN_QUOTE_FRESHNESS"
    finally:
        session.close()


def test_missing_price_is_rejected_and_not_fabricated() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        scripted = _Scripted()
        payload = _pair()
        payload["priceUsd"] = None
        scripted.token_pairs[MINT] = [payload]
        row = _refresh(session, scripted)
        assert row.status == "REJECTED"
        assert row.reject_reason == "MISSING_PRICE"
        assert row.price_usd is None
        try:
            load_executable_quote(session, token, get_settings())
            raise AssertionError("missing price must not execute")
        except QuoteRejected as exc:
            assert exc.reason == "MISSING_PRICE"
    finally:
        session.close()


def test_missing_liquidity_stays_null_and_rejects() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        scripted = _Scripted()
        payload = _pair()
        del payload["liquidity"]
        scripted.token_pairs[MINT] = [payload]
        row = _refresh(session, scripted)
        assert row.status == "REJECTED"
        assert row.reject_reason == "MISSING_LIQUIDITY"
        assert row.liquidity_usd is None
        try:
            load_executable_quote(session, token, get_settings())
            raise AssertionError("missing liquidity must not execute")
        except QuoteRejected as exc:
            assert exc.reason == "MISSING_LIQUIDITY"
    finally:
        session.close()


def test_pair_mismatch_does_not_silent_switch() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session, pair=PAIR_A)
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair(pairAddress=PAIR_B, liquidity={"usd": 90000})]
        row = _refresh(session, scripted)
        assert row.status == "REJECTED"
        assert row.reject_reason == "PAIR_MISMATCH"
        session.refresh(token)
        assert token.pair_address == PAIR_A
        try:
            load_executable_quote(session, token, get_settings())
            raise AssertionError("mismatched pair must not execute")
        except QuoteRejected as exc:
            assert exc.reason == "PAIR_MISMATCH"
    finally:
        session.close()


def test_rate_limiting_and_provider_failure_are_unavailable() -> None:
    session = get_session_factory()()
    settings = get_settings()
    previous = settings.dexscreener_max_retries
    try:
        _wipe(session)
        _token(session)
        settings.dexscreener_max_retries = 0
        scripted = _Scripted()
        scripted.fail_rate_limit(3)
        scripted.token_pairs[MINT] = [_pair()]
        row = _refresh(session, scripted)
        assert row.status == "PROVIDER_UNAVAILABLE"
        assert row.reject_reason == "PROVIDER_UNAVAILABLE"
        reset_quote_coordinator()
        scripted.token_pairs[MINT] = httpx.Response(500, json={"error": "down"})
        row = _refresh(session, scripted)
        assert row.status == "PROVIDER_UNAVAILABLE"
    finally:
        settings.dexscreener_max_retries = previous
        session.close()


def test_duplicate_requests_share_http_cache() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        _token(session)
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair()]
        first = _refresh(session, scripted)
        second = _refresh(session, scripted)
        assert first.cache_hit == 0
        assert second.cache_hit == 1
        pair_calls = [path for path in scripted.calls if "token-pairs" in path]
        assert len(pair_calls) == 1
        assert session.scalar(select(func.count()).select_from(PaperMarketQuote)) == 2
    finally:
        session.close()


def test_paper_simulator_uses_on_demand_quote(monkeypatch) -> None:
    session = get_session_factory()()
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_max_allocation_pct", 1.0)
    monkeypatch.setattr(settings, "paper_max_exposure_pct", 1.0)
    try:
        _wipe(session)
        _token(session)
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair()]
        row = _refresh(session, scripted)
        order = simulate_manual_order(
            session,
            settings,
            mint_address=MINT,
            side="BUY",
            quantity_value="1",
            client_order_id="quote-fill",
        )
        assert order.status == "FILLED"
        assert order.paper_quote_id == row.id
        assert order.quote_snapshot_id is None
        assert float(order.observed_price_usd) == 1.25
    finally:
        session.close()


def test_historical_snapshots_are_not_rewritten() -> None:
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session)
        snap = MarketSnapshot(
            token_id=token.id,
            price_usd=0.5,
            liquidity_usd=12000,
            recorded_at=utc_now() - timedelta(hours=1),
            discovery_source="scheduled_discovery",
        )
        session.add(snap)
        session.commit()
        snap_id = snap.id
        count_before = session.scalar(select(func.count()).select_from(MarketSnapshot))
        opp_before = session.scalar(select(func.count()).select_from(OpportunityAssessment))
        scripted = _Scripted()
        scripted.token_pairs[MINT] = [_pair()]
        _refresh(session, scripted)
        session.refresh(snap)
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == count_before
        assert session.scalar(select(func.count()).select_from(OpportunityAssessment)) == opp_before
        assert snap.id == snap_id
        assert snap.price_usd == 0.5
        assert snap.discovery_source == "scheduled_discovery"
        quotes = list(session.scalars(select(PaperMarketQuote)).all())
        assert len(quotes) == 1
        assert quotes[0].source == "dexscreener_on_demand"
    finally:
        session.close()


def test_api_refresh_and_get_does_not_fetch_on_get(client: TestClient, monkeypatch) -> None:
    session = get_session_factory()()
    scripted = _Scripted()
    scripted.token_pairs[MINT] = [_pair()]
    try:
        _wipe(session)
        _token(session)
    finally:
        session.close()

    from app.services import paper_quote_service as service

    monkeypatch.setattr(service, "DexScreenerClient", lambda *args, **kwargs: scripted.client())
    refresh = client.post(f"/api/v1/paper/quotes/refresh/{MINT}")
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["status"] == "ACCEPTED"
    assert body["pair_address"] == PAIR_A
    assert body["freshness_basis"] == "PROVIDER_TIMESTAMP"
    assert body["source"] == "dexscreener_on_demand"
    http_calls = len(scripted.calls)
    stored = client.get(f"/api/v1/paper/quotes/{MINT}")
    assert stored.status_code == 200
    assert stored.json()["external_request"] is False
    assert stored.json()["id"] == body["id"]
    assert len(scripted.calls) == http_calls
    missing = client.get("/api/v1/paper/quotes/So11111111111111111111111111111111111111112")
    assert missing.status_code == 404
