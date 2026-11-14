"""Phase 9C.6.3 shared RPC session, cooldown persistence, and batch safety."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    CreatorObservation,
    MarketSnapshot,
    MetaplexCreatorObservation,
    OpportunityAssessment,
    RpcCooldownState,
    Token,
    TokenAccountOwnerObservation,
)
from app.services.creator_evidence_batch import run_creator_evidence_batch
from app.services.creator_scan import persist_creator_observation, scan_creator_tokens
from app.services.holder_owner_scan import scan_holder_owners
from app.services.metaplex_creator_scan import scan_metaplex_creators
from app.services.mint_initialization import MintInitInspection, inspect_mint_initialization
from app.services.rpc_accounting import KIND_COOLDOWN_ACTIVE, KIND_RATE_LIMITED
from app.services.rpc_cooldown import (
    compute_cooldown_seconds,
    get_cooldown_store,
)
from app.services.rpc_session import (
    get_shared_rpc_session,
    reset_shared_rpc_session,
    shutdown_shared_rpc_session,
)
from app.services.solana_rpc import SolanaRpcError
from app.timeutil import utc_now
from tests.dexscreener_fakes import run
from tests.test_rpc_reliability import TOKEN_B
from tests.test_security import MINT, RpcScript, _clean

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _seed_tokens(session, extra: bool = True) -> None:
    _clean(session)
    session.add(
        Token(token_address=MINT, symbol="A", name="Alpha", chain="solana", last_discovered_at=NOW)
    )
    if extra:
        session.add(
            Token(
                token_address=TOKEN_B,
                symbol="B",
                name="Beta",
                chain="solana",
                last_discovered_at=NOW - timedelta(days=1),
            )
        )
    session.commit()


def _activate_cooldown(*, seconds: float = 60, consecutive: int = 3) -> None:
    get_cooldown_store().activate(
        until_at=utc_now() + timedelta(seconds=seconds),
        reason=KIND_RATE_LIMITED,
        retry_after_seconds=None,
        backoff_seconds=seconds,
        consecutive_429=consecutive,
    )


def test_shared_client_reused_across_three_evidence_services() -> None:
    session = get_session_factory()()
    try:
        _seed_tokens(session, extra=False)
        script = RpcScript()
        script.signatures = []

        async def body():
            manager = reset_shared_rpc_session()
            manager.default_transport = httpx.MockTransport(script.handler)
            settings = get_settings()
            rpc = await manager.get_client(settings)
            first = await scan_creator_tokens(session, rpc, settings, token_address=MINT, limit=1)
            second = await scan_holder_owners(session, rpc, settings, token_address=MINT, limit=1)
            third = await scan_metaplex_creators(session, rpc, settings, token_address=MINT, limit=1)
            same = manager.client is rpc
            await shutdown_shared_rpc_session()
            return first, second, third, same, rpc

        first, second, third, same, rpc = run(body())
        assert same is True
        assert first.tokens_scanned == 1
        assert second.tokens_scanned == 1
        assert third.tokens_scanned == 1
        assert rpc.accounting.attempts == first.rpc_calls + second.rpc_calls + third.rpc_calls
    finally:
        session.close()


def test_concurrent_get_client_does_not_create_duplicates() -> None:
    script = RpcScript()

    async def body():
        manager = reset_shared_rpc_session()
        manager.default_transport = httpx.MockTransport(script.handler)
        first, second = await asyncio.gather(
            manager.get_client(get_settings()),
            manager.get_client(get_settings()),
        )
        assert first is second
        await shutdown_shared_rpc_session()
        return first

    client = run(body())
    assert client is not None


def test_no_rpc_during_cooldown_and_accurate_attempts() -> None:
    script = RpcScript()
    script.signatures = []
    _activate_cooldown()

    async def body():
        async with script.client() as rpc:
            with pytest.raises(SolanaRpcError) as caught:
                await rpc.get_signatures_for_address(MINT, limit=10)
            return caught.value, rpc.accounting.copy(), list(script.calls)

    error, accounting, calls = run(body())
    assert error.kind == KIND_COOLDOWN_ACTIVE
    assert accounting.attempts == 0
    assert accounting.retries == 0
    assert calls == []


def test_cooldown_persists_across_application_restart() -> None:
    script = RpcScript()
    _activate_cooldown(seconds=120)

    async def body():
        manager = reset_shared_rpc_session()
        manager.default_transport = httpx.MockTransport(script.handler)
        first = await manager.get_client(get_settings())
        with pytest.raises(SolanaRpcError) as caught:
            await first.get_account_info(MINT)
        assert caught.value.kind == KIND_COOLDOWN_ACTIVE
        await shutdown_shared_rpc_session()
        restarted = reset_shared_rpc_session()
        restarted.default_transport = httpx.MockTransport(script.handler)
        second = await restarted.get_client(get_settings())
        assert first is not second
        with pytest.raises(SolanaRpcError) as caught_again:
            await second.get_account_info(MINT)
        assert caught_again.value.kind == KIND_COOLDOWN_ACTIVE
        await shutdown_shared_rpc_session()

    run(body())
    assert script.calls == []


def test_retry_after_sets_cooldown_duration(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.services.solana_rpc.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.services.solana_rpc.random.uniform", lambda _a, _b: 1.0)
    settings = get_settings()
    monkeypatch.setattr(settings, "solana_rpc_max_backoff_seconds", 5.0)
    monkeypatch.setattr(settings, "solana_rpc_cooldown_max_seconds", 60.0)

    script = RpcScript()
    original = script.handler

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        script.calls.append(payload.get("method"))
        return httpx.Response(429, headers={"Retry-After": "4"}, json={"error": "rate"})

    script.handler = handler  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            with pytest.raises(SolanaRpcError):
                await rpc.get_signatures_for_address(MINT, limit=10)
            return rpc.accounting.copy(), get_cooldown_store().snapshot()

    accounting, cooldown = run(body())
    assert accounting.stop_reason == KIND_RATE_LIMITED
    assert accounting.attempts == 3
    assert accounting.retries == 2
    assert accounting.http_429 == 3
    assert accounting.failures == 3
    assert sleeps == [4.0, 4.0]
    assert cooldown.active is True
    assert cooldown.retry_after_seconds == 4.0
    assert cooldown.backoff_seconds == 4.0


def test_bounded_exponential_cooldown_without_retry_after() -> None:
    settings = get_settings()
    wait = compute_cooldown_seconds(settings, None, 3)
    assert wait == min(
        settings.solana_rpc_cooldown_base_seconds * (2 ** 2),
        settings.solana_rpc_cooldown_max_seconds,
    )
    capped = compute_cooldown_seconds(settings, 9999.0, 3)
    assert capped == settings.solana_rpc_cooldown_max_seconds


def test_rate_limited_batch_skips_remaining_ops_and_second_token() -> None:
    session = get_session_factory()()
    try:
        _seed_tokens(session)
        script = RpcScript()
        script._rate_remaining = 10

        async def body():
            async with script.client() as rpc:
                summary = await run_creator_evidence_batch(session, rpc, get_settings(), limit=5)
                return summary, rpc.accounting.copy()

        summary, accounting = run(body())
        assert summary.status == "partial"
        assert summary.stop_reason == KIND_RATE_LIMITED
        assert summary.stop_token == MINT
        assert MINT in summary.tokens_started
        assert TOKEN_B in summary.tokens_skipped
        names = [(item.name, item.status, item.token_address) for item in summary.operations]
        assert ("mint_init", "partial", MINT) in names or ("mint_init", "stopped", MINT) in names
        assert any(item.name == "holders" and item.status == "skipped" and item.token_address == MINT for item in summary.operations)
        assert any(item.name == "metaplex" and item.status == "skipped" and item.token_address == MINT for item in summary.operations)
        assert all(item.token_address != TOKEN_B or item.status == "skipped" for item in summary.operations)
        assert accounting.attempts == summary.rpc_calls
        assert script.calls.count("getAccountInfo") == 0
        holders = list(session.scalars(select(TokenAccountOwnerObservation)).all())
        meta = list(session.scalars(select(MetaplexCreatorObservation)).all())
        assert holders == []
        assert meta == []
    finally:
        session.close()


def test_attempted_at_not_observed_at_on_failure_and_append_only() -> None:
    session = get_session_factory()()
    try:
        _seed_tokens(session, extra=False)
        existing = persist_creator_observation(
            session,
            session.scalar(select(Token).where(Token.token_address == MINT)),
            MintInitInspection(mint=MINT, inspection_status="UNKNOWN", mint_init_status="UNKNOWN"),
        )
        recorded = existing.recorded_at
        prior_id = existing.id
        script = RpcScript()
        script._rate_remaining = 10

        async def body():
            async with script.client() as rpc:
                return await inspect_mint_initialization(MINT, rpc)

        result = run(body())
        assert result.attempted_at is not None
        assert result.observed_at is None
        assert result.rpc_calls == result.rpc_successes + result.rpc_failures
        assert result.rpc_retries == 2
        row = persist_creator_observation(
            session,
            session.scalar(select(Token).where(Token.token_address == MINT)),
            result,
        )
        assert row.id != prior_id
        original = session.get(CreatorObservation, prior_id)
        assert original is not None
        assert original.recorded_at == recorded
        assert original.observed_at is None
    finally:
        session.close()


def test_individual_endpoints_cannot_bypass_cooldown(client: TestClient) -> None:
    _activate_cooldown()
    for path in (
        "/api/v1/creator/scan",
        "/api/v1/creator/holders/scan",
        "/api/v1/creator/metadata/scan",
        "/api/v1/creator/batch",
    ):
        response = client.post(path, json={"limit": 1})
        assert response.status_code == 429
        body = response.json()["detail"]
        assert body["status"] == KIND_COOLDOWN_ACTIVE
        assert body["rpc_calls"] == 0
        assert body["active"] is True


def test_cooldown_persists_across_separate_http_requests(client: TestClient) -> None:
    _activate_cooldown()
    first = client.post("/api/v1/creator/scan", json={"limit": 1})
    second = client.get("/api/v1/creator/rpc/cooldown")
    third = client.post("/api/v1/creator/batch", json={"limit": 1})
    assert first.status_code == 429
    assert second.status_code == 200
    assert second.json()["active"] is True
    assert third.status_code == 429


def test_conflicting_batch_returns_409(client: TestClient) -> None:
    manager = get_shared_rpc_session()
    manager._busy = True
    response = client.post("/api/v1/creator/batch", json={"limit": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "BATCH_IN_PROGRESS"


def test_shutdown_rejects_use() -> None:
    async def body():
        manager = reset_shared_rpc_session()
        await manager.shutdown()
        with pytest.raises(SolanaRpcError) as caught:
            await manager.get_client(get_settings())
        assert "shut down" in str(caught.value)

    run(body())


def test_cooldown_blocked_scan_does_not_rewrite_history() -> None:
    session = get_session_factory()()
    try:
        _seed_tokens(session, extra=False)
        token = session.scalar(select(Token).where(Token.token_address == MINT))
        persist_creator_observation(
            session,
            token,
            MintInitInspection(mint=MINT, inspection_status="UNKNOWN", mint_init_status="UNKNOWN"),
        )
        creator_ids = [row.id for row in session.scalars(select(CreatorObservation)).all()]
        snap_count = session.scalar(select(func.count()).select_from(MarketSnapshot))
        opp_count = len(list(session.scalars(select(OpportunityAssessment)).all()))
        _activate_cooldown()

        async def body():
            async with RpcScript().client() as rpc:
                return await scan_creator_tokens(session, rpc, get_settings(), token_address=MINT, limit=1)

        summary = run(body())
        assert summary.status == "stopped"
        assert summary.stop_reason == KIND_COOLDOWN_ACTIVE
        assert summary.rpc_calls == 0
        assert summary.tokens_scanned == 0
        after = [row.id for row in session.scalars(select(CreatorObservation)).all()]
        assert after == creator_ids
        assert len(list(session.scalars(select(OpportunityAssessment)).all())) == opp_count
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == snap_count
    finally:
        session.close()


def test_expired_cooldown_is_history_not_a_block(client: TestClient) -> None:
    store = get_cooldown_store()
    past = utc_now() - timedelta(hours=1)
    store.activate(
        until_at=past,
        reason=KIND_RATE_LIMITED,
        retry_after_seconds=10.0,
        backoff_seconds=10.0,
        consecutive_429=3,
    )
    session = get_session_factory()()
    try:
        row = session.get(RpcCooldownState, "solana_rpc")
        assert row is not None
        row.cleared_at = None
        row.until_at = past
        session.commit()
    finally:
        session.close()
    snap = store.snapshot()
    assert snap.active is False
    assert snap.blocking is False
    assert snap.expired is True
    assert snap.cleared_at is not None
    payload = client.get("/api/v1/creator/rpc/cooldown").json()
    assert payload["active"] is False
    assert payload["blocking"] is False
    assert payload["status"] is None
    assert payload["expired"] is True
    assert payload["cleared_at"] is not None
