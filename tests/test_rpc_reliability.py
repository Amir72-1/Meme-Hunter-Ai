"""Phase 9C.6.1 RPC accounting, pagination, batch-stop, and attempt timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    CreatorObservation,
    IntelligenceScan,
    Token,
    TokenAccountOwnerObservation,
)
from app.services.creator_scan import persist_creator_observation, scan_creator_tokens
from app.services.holder_owner_scan import scan_holder_owners
from app.services.metaplex_creator_scan import scan_metaplex_creators
from app.services.mint_initialization import (
    MAX_TX_FETCH,
    SIGNATURE_PAGE_SIZE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    MintInitInspection,
    inspect_mint_initialization,
    latest_observation_as_of,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from app.services.rpc_accounting import KIND_RATE_LIMITED
from app.services.solana_rpc import SolanaRpcError
from app.services.token_account_ownership import (
    inspect_token_account_ownership,
    latest_account_observations_as_of,
)
from app.timeutil import as_utc
from tests.dexscreener_fakes import run
from tests.test_mint_initialization import INIT, SIG_OLD, _history, _init_tx, _sigs, _transfer_tx
from tests.test_security import MINT, RpcScript, _clean
from tests.test_token_account_ownership import ATA_A, _largest, _script, _system_account, _token_account

NOW = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
TOKEN_B = "Mint222222222222222222222222222222222222222"


def _sig_page(start: int, count: int) -> list[dict]:
    return [{"signature": f"S{start + index:06d}" + ("1" * 50), "slot": start + index} for index in range(count)]


def test_rpc_attempts_include_retries() -> None:
    script = RpcScript()
    script._rate_remaining = 2
    script.signatures = []

    async def body():
        async with script.client() as rpc:
            result = await inspect_mint_initialization(MINT, rpc)
            return result, rpc.accounting.copy()

    result, accounting = run(body())
    assert result.rpc_calls == 3
    assert result.rpc_retries == 2
    assert result.rpc_http_429 == 2
    assert result.rpc_successes == 1
    assert accounting.attempts == 3
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.observed_at is None
    assert result.attempted_at is not None


def test_429_classification_and_exhausted_retries() -> None:
    script = RpcScript()
    script._rate_remaining = 10

    async def body():
        async with script.client() as rpc:
            try:
                await rpc.get_signatures_for_address(MINT, limit=10)
            except SolanaRpcError as exc:
                return exc, rpc.accounting.copy()
            return None, rpc.accounting.copy()

    error, accounting = run(body())
    assert error is not None
    assert error.is_rate_limited
    assert error.kind == KIND_RATE_LIMITED
    assert error.status_code == 429
    assert accounting.attempts == 3
    assert accounting.failures == 3
    assert accounting.http_429 == 3
    assert accounting.successes == 0
    assert accounting.stop_reason == KIND_RATE_LIMITED


def test_retry_after_is_respected(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.services.solana_rpc.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.services.solana_rpc.random.uniform", lambda _a, _b: 1.0)
    settings = get_settings()
    monkeypatch.setattr(settings, "solana_rpc_max_backoff_seconds", 5.0)

    script = RpcScript()
    original = script.handler

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        script.calls.append(payload.get("method"))
        if getattr(script, "_rate_remaining", 0) > 0:
            script._rate_remaining -= 1
            return httpx.Response(429, headers={"Retry-After": "1.5"}, json={"error": "rate"})
        return original(request)

    script.handler = handler  # type: ignore[method-assign]
    script._rate_remaining = 1
    script.signatures = []

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    run(body())
    assert sleeps
    assert sleeps[0] == 1.5


def test_bounded_backoff_caps_wait(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.services.solana_rpc.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.services.solana_rpc.random.uniform", lambda _a, _b: 1.0)
    settings = get_settings()
    monkeypatch.setattr(settings, "solana_rpc_max_backoff_seconds", 0.25)

    script = RpcScript()

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    script.handler = boom  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.rpc_timeouts == 3
    assert result.rpc_calls == 3
    assert sleeps == [0.25, 0.25]


def test_batch_stops_on_repeated_429_and_skips_later_tokens() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.commit()
        session.add_all(
            [
                Token(
                    token_address=MINT,
                    symbol="A",
                    name="Alpha",
                    chain="solana",
                    last_discovered_at=NOW,
                ),
                Token(
                    token_address=TOKEN_B,
                    symbol="B",
                    name="Beta",
                    chain="solana",
                    last_discovered_at=NOW - timedelta(days=1),
                ),
            ]
        )
        session.commit()
        script = RpcScript()
        script._rate_remaining = 10

        async def body():
            async with script.client() as rpc:
                first = await scan_creator_tokens(session, rpc, get_settings(), limit=5)
                second = await scan_metaplex_creators(session, rpc, get_settings(), limit=5)
                return first, second, rpc.accounting.copy()

        first, second, accounting = run(body())
        assert first.status == "partial"
        assert first.stop_reason == KIND_RATE_LIMITED
        assert first.stop_token == MINT
        assert first.tokens_scanned == 1
        assert first.rpc_calls >= 3
        assert first.rpc_http_429 >= 2
        assert second.status == "stopped"
        assert second.tokens_scanned == 0
        rows = list(session.scalars(select(CreatorObservation)).all())
        assert len(rows) == 1
        assert rows[0].mint_address == MINT
        assert rows[0].observed_at is None
        assert rows[0].attempted_at is not None
        assert accounting.stop_reason == KIND_RATE_LIMITED
        assert script.calls.count("getAccountInfo") == 0
    finally:
        session.close()


def test_signature_pagination_full_then_short_page(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "creator_signature_page_limit", 5)
    script = RpcScript()
    script.signature_pages = [_sig_page(0, SIGNATURE_PAGE_SIZE), _sig_page(1000, 12)]
    script.transaction = _transfer_tx(mint=MINT)

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.signature_pages == 2
    assert result.signatures_retrieved == SIGNATURE_PAGE_SIZE + 12
    assert result.history_incomplete is False
    assert result.transactions_inspected == MAX_TX_FETCH
    assert result.transactions_skipped == SIGNATURE_PAGE_SIZE + 12 - MAX_TX_FETCH
    assert script.calls.count("getSignaturesForAddress") == 2
    assert script.calls.count("getTransaction") == MAX_TX_FETCH
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.inspection_status != "VERIFIED_ABSENT"


def test_max_page_truncation_is_unknown_not_absent(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "creator_signature_page_limit", 2)
    script = RpcScript()
    script.signature_pages = [_sig_page(0, SIGNATURE_PAGE_SIZE), _sig_page(1000, SIGNATURE_PAGE_SIZE)]
    script.transactions = {SIG_OLD: _init_tx(mint=MINT, extra_signers=[INIT])}

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.history_incomplete is True
    assert result.signature_pages == 2
    assert result.signatures_retrieved == SIGNATURE_PAGE_SIZE * 2
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.mint_init_status == STATUS_UNKNOWN
    assert result.mint_init_signer is None
    assert result.observed_at is None
    assert "truncated" in (result.parser_notes or "").lower()
    assert result.inspection_status != "VERIFIED_ABSENT"
    assert result.mint_init_status != "VERIFIED_ABSENT"
    assert script.calls.count("getTransaction") == 0
    assert result.transactions_skipped == SIGNATURE_PAGE_SIZE * 2


def test_short_page_terminates_pagination(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "creator_signature_page_limit", 20)
    script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: _init_tx(mint=MINT, extra_signers=[INIT])})

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.history_incomplete is False
    assert result.signature_pages == 1
    assert result.signatures_retrieved == 1
    assert result.inspection_status == "VERIFIED"
    assert result.transactions_inspected == 1
    assert result.transactions_skipped == 0


def test_missing_archival_history_is_not_verified_absent() -> None:
    script = RpcScript()
    script.signatures = None

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.observed_at is None
    assert result.inspection_status != "VERIFIED_ABSENT"
    assert "absent" in (result.parser_notes or "").lower()


def test_transaction_inspection_stays_bounded() -> None:
    signatures = [f"T{index:03d}" + ("2" * 50) for index in range(8)]
    script = RpcScript()
    script.signatures = _sigs(*signatures)
    script.transactions = {item: _transfer_tx(mint=MINT) for item in signatures}

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.transactions_inspected == MAX_TX_FETCH
    assert result.transactions_skipped == 3
    assert script.calls.count("getTransaction") == MAX_TX_FETCH


def test_holder_rpc_accounting_counts_failed_largest_accounts() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account("Frz1111111111111111111111111111111111111111")})
    script._rate_remaining = 10

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.rpc_calls == 3
    assert result.rpc_retries == 2
    assert result.rpc_http_429 == 3
    assert result.largest_account_requests == 3
    assert result.account_info_requests == 0
    assert result.owner_lookups == 0
    assert result.records[0].classification_status == STATUS_UNAVAILABLE
    assert result.records[0].observed_at is None
    assert result.records[0].attempted_at is not None


def test_failed_attempt_timestamp_is_not_observed_at() -> None:
    script = RpcScript()

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    script.handler = boom  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.observed_at is None
    assert result.attempted_at is not None
    assert result.rpc_timeouts == 3
    assert result.rpc_calls == 3


def test_point_in_time_still_uses_observed_at_only() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.execute(delete(TokenAccountOwnerObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        failed = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status=STATUS_UNAVAILABLE,
            mint_init_status=STATUS_UNAVAILABLE,
            observed_at=None,
            attempted_at=NOW + timedelta(hours=1),
            recorded_at=NOW + timedelta(hours=1),
            source="solana_rpc",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        later = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status="VERIFIED",
            mint_init_status="VERIFIED",
            mint_init_signer=INIT,
            observed_at=NOW + timedelta(hours=2),
            attempted_at=NOW + timedelta(hours=2),
            recorded_at=NOW + timedelta(hours=2),
            source="solana_rpc",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        holder_fail = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=MINT,
            owner_classification="UNKNOWN",
            classification_status=STATUS_UNAVAILABLE,
            observed_at=None,
            attempted_at=NOW + timedelta(hours=3),
            recorded_at=NOW + timedelta(hours=3),
            source="solana_rpc",
            errors_json="[]",
        )
        session.add_all([failed, later, holder_fail])
        session.commit()
        as_of = latest_observation_as_of(session, token.id, NOW + timedelta(hours=3))
        assert as_of is not None
        assert as_of.inspection_status == "VERIFIED"
        early = latest_observation_as_of(session, token.id, NOW + timedelta(minutes=30))
        assert early is None
        holders = latest_account_observations_as_of(session, token.id, NOW + timedelta(hours=4))
        assert holders == []
    finally:
        session.close()


def test_append_only_observations() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        first = persist_creator_observation(
            session,
            token,
            MintInitInspection(
                mint=MINT,
                inspection_status=STATUS_UNKNOWN,
                mint_init_status=STATUS_UNKNOWN,
                attempted_at=NOW,
                observed_at=None,
            ),
            recorded_at=NOW,
        )
        second = persist_creator_observation(
            session,
            token,
            MintInitInspection(
                mint=MINT,
                inspection_status=STATUS_UNKNOWN,
                mint_init_status=STATUS_UNKNOWN,
                attempted_at=NOW + timedelta(minutes=1),
                observed_at=None,
            ),
            recorded_at=NOW + timedelta(minutes=1),
        )
        rows = list(session.scalars(select(CreatorObservation).order_by(CreatorObservation.id)).all())
        assert len(rows) == 2
        assert rows[0].id == first.id
        assert rows[1].id == second.id
        assert as_utc(rows[0].attempted_at) == NOW
        assert rows[0].observed_at is None
    finally:
        session.close()


def test_historical_evaluation_and_opportunity_floors_unchanged() -> None:
    settings = get_settings()
    assert settings.opportunity_min_completeness == 0.40
    assert settings.opportunity_min_confidence == 0.50
    assert settings.opportunity_high_threshold == 70
    assert settings.security_min_completeness == 0.50
    assert settings.security_min_confidence == 0.50
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=90,
        opportunity_score=90,
        data_completeness=0.39,
        confidence=0.90,
    )
    assert status != "SCORED"
    assert label != HIGH_OPPORTUNITY


def test_holder_scan_stop_does_not_rewrite_committed_rows() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(TokenAccountOwnerObservation))
        session.commit()
        session.add(
            Token(token_address=MINT, symbol="A", name="Alpha", chain="solana", last_discovered_at=NOW)
        )
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
        script = _script(_largest(ATA_A), {ATA_A: _token_account("Frz1111111111111111111111111111111111111111"), "Frz1111111111111111111111111111111111111111": _system_account()})
        script._rate_remaining = 10

        async def body():
            async with script.client() as rpc:
                return await scan_holder_owners(session, rpc, get_settings(), limit=5)

        summary = run(body())
        assert summary.status == "partial"
        assert summary.tokens_scanned == 1
        assert summary.stop_token == MINT
        rows = list(session.scalars(select(TokenAccountOwnerObservation)).all())
        assert len(rows) == 1
        assert rows[0].observed_at is None
        assert rows[0].attempted_at is not None
        scan = session.scalar(select(IntelligenceScan).where(IntelligenceScan.scan_type == "creator_holders"))
        assert scan is not None
        assert scan.status == "partial"
    finally:
        session.close()
