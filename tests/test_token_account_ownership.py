"""Phase 9C.4C token-account owner classification tests."""

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
from app.services.holder_owner_scan import scan_holder_owners
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from app.services.token_account_ownership import (
    AMM_PROGRAM_IDS,
    CLASS_AMM,
    CLASS_BURN,
    CLASS_PROTOCOL,
    CLASS_UNKNOWN,
    CLASS_WALLET,
    MAX_TOKEN_ACCOUNTS,
    PROTOCOL_PROGRAM_IDS,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    SYSTEM_PROGRAM,
    inspect_token_account_ownership,
    latest_account_observations_as_of,
)
from app.services.token_security import TOKEN_PROGRAM
from app.timeutil import as_utc
from tests.dexscreener_fakes import run
from tests.test_security import BURN, FREEZE, MINT, RpcScript, _clean

ATA_A = "Hold11111111111111111111111111111111111111"
ATA_B = "Hold22222222222222222222222222222222222222"
ATA_C = "Hold33333333333333333333333333333333333333"
RAYDIUM = next(iter(AMM_PROGRAM_IDS))
METAPLEX = next(iter(PROTOCOL_PROGRAM_IDS))
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def _token_account(owner: str, mint: str = MINT, amount: float = 12.5, state: str = "initialized") -> dict:
    return {
        "owner": TOKEN_PROGRAM,
        "data": {
            "program": "spl-token",
            "parsed": {
                "type": "account",
                "info": {
                    "mint": mint,
                    "owner": owner,
                    "state": state,
                    "tokenAmount": {"amount": "12500000", "decimals": 6, "uiAmount": amount},
                },
            },
        },
    }


def _system_account() -> dict:
    return {"owner": SYSTEM_PROGRAM, "lamports": 1, "executable": False, "data": {"program": "system"}}


def _program_account(program_id: str) -> dict:
    return {"owner": program_id, "lamports": 1, "executable": False, "data": {"program": "unknown"}}


def _largest(*addresses: str) -> list[dict]:
    return [{"address": item, "uiAmount": 10 + index} for index, item in enumerate(addresses)]


def _script(largest: list[dict], accounts: dict) -> RpcScript:
    script = RpcScript()
    script.largest = largest
    script.accounts = accounts
    return script


def test_successful_wallet_owner_resolution() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account(FREEZE), FREEZE: _system_account()})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.token_account_address == ATA_A
    assert record.token_account_owner == FREEZE
    assert record.token_account_amount == 12.5
    assert record.token_account_status == "initialized"
    assert record.owner_classification == CLASS_WALLET
    assert record.classification_status == STATUS_VERIFIED
    assert record.observed_at is not None
    assert ATA_A != FREEZE
    assert script.calls == ["getTokenLargestAccounts", "getAccountInfo", "getAccountInfo"]


def test_multiple_accounts_same_owner_share_lookup() -> None:
    script = _script(
        _largest(ATA_A, ATA_B),
        {ATA_A: _token_account(FREEZE, amount=1), ATA_B: _token_account(FREEZE, amount=2), FREEZE: _system_account()},
    )

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.accounts_resolved == 2
    assert {item.token_account_owner for item in result.records} == {FREEZE}
    assert all(item.owner_classification == CLASS_WALLET for item in result.records)
    assert result.owner_lookups == 1
    assert script.calls.count("getAccountInfo") == 3


def test_missing_account_information_is_unavailable() -> None:
    script = _script(_largest(ATA_A), {ATA_A: None})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.classification_status == STATUS_UNAVAILABLE
    assert record.owner_classification == CLASS_UNKNOWN
    assert record.token_account_owner is None
    assert record.observed_at is None


def test_unsupported_account_data_is_unknown() -> None:
    script = _script(
        _largest(ATA_A),
        {ATA_A: {"owner": "11111111111111111111111111111111", "data": {"parsed": {"type": "mint", "info": {}}}}},
    )

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.owner_classification == CLASS_UNKNOWN
    assert record.classification_status == STATUS_UNKNOWN
    assert record.token_account_owner is None


def test_burn_address_evidence() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account(BURN)})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.owner_classification == CLASS_BURN
    assert record.classification_status == STATUS_VERIFIED
    assert record.token_account_owner == BURN
    assert script.calls == ["getTokenLargestAccounts", "getAccountInfo"]


def test_known_amm_vault_evidence() -> None:
    vault = "Vault11111111111111111111111111111111111111"
    script = _script(
        _largest(ATA_A),
        {ATA_A: _token_account(vault), vault: _program_account(RAYDIUM)},
    )

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.owner_classification == CLASS_AMM
    assert record.classification_status == STATUS_VERIFIED
    assert "large" in (record.classification_evidence or "").lower()


def test_known_protocol_account_evidence() -> None:
    pda = "Proto11111111111111111111111111111111111111"
    script = _script(_largest(ATA_A), {ATA_A: _token_account(pda), pda: _program_account(METAPLEX)})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.owner_classification == CLASS_PROTOCOL
    assert record.classification_status == STATUS_VERIFIED


def test_unknown_owner_program_is_unknown() -> None:
    other = "Other11111111111111111111111111111111111111"
    mystery = "Mystery11111111111111111111111111111111111"
    script = _script(_largest(ATA_A), {ATA_A: _token_account(other), other: _program_account(mystery)})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    record = result.records[0]
    assert record.owner_classification == CLASS_UNKNOWN
    assert record.classification_status == STATUS_UNKNOWN
    assert record.token_account_owner == other


def test_rpc_timeout_is_unavailable() -> None:
    script = RpcScript()

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    script.handler = boom  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.records[0].classification_status == STATUS_UNAVAILABLE


def test_rpc_429_is_unavailable() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account(FREEZE), FREEZE: _system_account()})
    script._rate_remaining = 10

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.records[0].classification_status == STATUS_UNAVAILABLE


def test_retry_then_success() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account(FREEZE), FREEZE: _system_account()})
    script._rate_remaining = 1

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.records[0].owner_classification == CLASS_WALLET
    assert script.calls[0] == "getTokenLargestAccounts"
    assert script.calls.count("getTokenLargestAccounts") >= 2


def test_rpc_budget_limits_account_lookups() -> None:
    addresses = [f"Acct{index:02d}{'1' * 40}" for index in range(25)]
    script = _script(_largest(*addresses), {item: _token_account(FREEZE) for item in addresses})
    script.accounts[FREEZE] = _system_account()

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    result = run(body())
    assert result.accounts_considered == MAX_TOKEN_ACCOUNTS
    assert script.calls.count("getTokenLargestAccounts") == 1
    assert script.calls.count("getAccountInfo") == MAX_TOKEN_ACCOUNTS + 1
    assert result.rpc_calls == 1 + MAX_TOKEN_ACCOUNTS + 1


def test_cache_preserves_observed_at() -> None:
    script = _script(_largest(ATA_A), {ATA_A: _token_account(FREEZE), FREEZE: _system_account()})
    client = script.client()
    client._cache_ttl = 60

    async def body():
        async with client as rpc:
            first = await inspect_token_account_ownership(MINT, rpc)
            second = await inspect_token_account_ownership(MINT, rpc)
            return first, second

    first, second = run(body())
    assert first.records[0].observed_at is not None
    assert second.records[0].observed_at == first.records[0].observed_at
    assert second.records[0].from_cache is True


def test_append_only_and_historical_as_of() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(TokenAccountOwnerObservation))
        session.execute(delete(CreatorObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        creator = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status="UNKNOWN",
            mint_init_status="UNKNOWN",
            source="solana_rpc",
            parser_notes="preexisting",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        session.add(creator)
        session.commit()
        creator_id = creator.id
        early = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=MINT,
            token_account_address=ATA_A,
            token_account_owner=None,
            owner_classification=CLASS_UNKNOWN,
            classification_status=STATUS_UNKNOWN,
            observed_at=NOW,
            recorded_at=NOW,
            source="solana_rpc",
            errors_json="[]",
        )
        later = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=MINT,
            token_account_address=ATA_A,
            token_account_owner=FREEZE,
            owner_classification=CLASS_WALLET,
            classification_status=STATUS_VERIFIED,
            observed_at=NOW + timedelta(hours=2),
            recorded_at=NOW + timedelta(hours=2),
            source="solana_rpc",
            errors_json="[]",
        )
        null_time = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=MINT,
            token_account_address=ATA_C,
            owner_classification=CLASS_UNKNOWN,
            classification_status=STATUS_UNAVAILABLE,
            observed_at=None,
            recorded_at=NOW,
            source="solana_rpc",
            errors_json="[]",
        )
        session.add_all([early, later, null_time])
        session.commit()
        as_of = latest_account_observations_as_of(session, token.id, NOW + timedelta(minutes=30))
        assert len(as_of) == 1
        assert as_of[0].owner_classification == CLASS_UNKNOWN
        assert as_of[0].token_account_owner is None
        excluded = latest_account_observations_as_of(session, token.id, NOW - timedelta(minutes=1))
        assert excluded == []
        latest = latest_account_observations_as_of(session, token.id, NOW + timedelta(hours=3))
        assert len(latest) == 1
        assert latest[0].owner_classification == CLASS_WALLET
        assert ATA_C not in {item.token_account_address for item in latest}
        still = session.get(CreatorObservation, creator_id)
        assert still is not None
        assert still.parser_notes == "preexisting"
        rows = list(session.scalars(select(TokenAccountOwnerObservation).order_by(TokenAccountOwnerObservation.id)).all())
        assert len(rows) == 3
        assert as_utc(rows[0].observed_at) == NOW
    finally:
        session.close()


def test_scan_and_api(client) -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(TokenAccountOwnerObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        script = _script(_largest(ATA_A), {ATA_A: _token_account(FREEZE), FREEZE: _system_account()})

        async def body():
            async with script.client() as rpc:
                return await scan_holder_owners(session, rpc, get_settings(), limit=5)

        summary = run(body())
        assert summary.tokens_scanned == 1
        assert summary.verified == 1
        assert summary.applied_limit == 5
        assert summary.max_scan_limit == 25
        scan = session.scalar(select(IntelligenceScan).where(IntelligenceScan.scan_type == "creator_holders"))
        assert scan is not None
    finally:
        session.close()

    status = client.get("/api/v1/creator/holders/status")
    assert status.status_code == 200
    payload = client.get(f"/api/v1/creator/holders/{MINT}").json()
    assert payload["observations"][0]["owner_classification"] == CLASS_WALLET
    assert payload["observations"][0]["token_account_address"] == ATA_A
    assert payload["observations"][0]["token_account_owner"] == FREEZE
    history = client.get(f"/api/v1/creator/holders/{MINT}/history")
    assert history.status_code == 200
    assert len(history.json()["observations"]) == 1
    missing = client.get("/api/v1/creator/holders/UnknownMint1111111111111111111111111111111")
    assert missing.status_code == 404


def test_opportunity_floors_unchanged() -> None:
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


def test_sample_unknown_fields() -> None:
    script = _script(_largest(ATA_A), {ATA_A: None})

    async def body():
        async with script.client() as rpc:
            return await inspect_token_account_ownership(MINT, rpc)

    record = run(body()).records[0]
    assert record.token_account_address == ATA_A
    assert record.token_account_owner is None
    assert record.owner_classification == CLASS_UNKNOWN
    assert record.classification_status == STATUS_UNAVAILABLE
    assert record.observed_at is None
    assert record.token_program is None
