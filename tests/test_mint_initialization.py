"""Phase 9C.4B mint initialization intelligence tests."""

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
    WalletActivity,
)
from app.services.creator_scan import (
    CREATOR_SCAN_HARD_MAX,
    persist_creator_observation,
    scan_creator_tokens,
)
from app.services.mint_initialization import (
    PUMP_CREATE_DISCRIMINATOR,
    PUMP_PROGRAM,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    inspect_mint_initialization,
    latest_observation_as_of,
    parse_mint_initialization_transaction,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from app.timeutil import as_utc
from tests.dexscreener_fakes import run
from tests.test_security import BURN, FREEZE, MINT, RpcScript, TOKEN_A, _clean

FEE = "FeePay1111111111111111111111111111111111111"
INIT = FREEZE
OTHER = BURN
SIG_OLD = "5oldSignature111111111111111111111111111111111111111111111111111111111111111"
SIG_NEW = "5newSignature111111111111111111111111111111111111111111111111111111111111111"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number > 0:
        number, rem = divmod(number, 58)
        out = _B58[rem] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return ("1" * pad) + (out or "1")


def _keys(*pairs: tuple[str, bool]) -> list[dict]:
    return [{"pubkey": pubkey, "signer": signer, "writable": True} for pubkey, signer in pairs]


def _init_tx(
    *,
    program_id: str = TOKEN_PROGRAM,
    ix_type: str = "initializeMint",
    mint: str = TOKEN_A,
    fee_payer: str = FEE,
    extra_signers: list[str] | None = None,
    include_extra_in_ix: bool = True,
    slot: int = 100,
    block_time: int = 1700000000,
    parsed: bool = True,
) -> dict:
    extra_signers = extra_signers or []
    keys = _keys((fee_payer, True), *[(item, True) for item in extra_signers], (mint, mint in extra_signers))
    ix_accounts = [mint]
    if include_extra_in_ix:
        ix_accounts = extra_signers + [mint]
    instruction: dict = {"programId": program_id, "accounts": ix_accounts}
    if parsed:
        instruction["program"] = "spl-token" if program_id == TOKEN_PROGRAM else "spl-token-2022"
        instruction["parsed"] = {
            "type": ix_type,
            "info": {"mint": mint, "decimals": 6, "mintAuthority": extra_signers[0] if extra_signers else fee_payer},
        }
    else:
        instruction["data"] = "unparsed"
    return {
        "slot": slot,
        "blockTime": block_time,
        "transaction": {
            "signatures": [SIG_OLD],
            "message": {"accountKeys": keys, "instructions": [instruction]},
        },
        "meta": {"err": None, "innerInstructions": [], "preBalances": [1, 1, 0], "postBalances": [1, 1, 1]},
    }


def _transfer_tx(mint: str = TOKEN_A) -> dict:
    return {
        "slot": 200,
        "blockTime": 1700000100,
        "transaction": {
            "signatures": [SIG_NEW],
            "message": {
                "accountKeys": _keys((FEE, True), (mint, False)),
                "instructions": [
                    {
                        "programId": TOKEN_PROGRAM,
                        "program": "spl-token",
                        "parsed": {"type": "amountToUiAmount", "info": {}},
                        "accounts": [mint],
                    }
                ],
            },
        },
        "meta": {"err": None, "innerInstructions": []},
    }


def _pump_tx(*, mint: str = TOKEN_A, user: str = INIT, fee_payer: str = FEE) -> dict:
    accounts = [
        mint,
        "MintAuth11111111111111111111111111111111111",
        "Bond111111111111111111111111111111111111111",
        "AssocB1111111111111111111111111111111111111",
        "Global1111111111111111111111111111111111111",
        "MetaPx1111111111111111111111111111111111111",
        "MetaAcc111111111111111111111111111111111111",
        user,
        "11111111111111111111111111111111",
        TOKEN_PROGRAM,
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "SysvarRent111111111111111111111111111111111",
        "Event11111111111111111111111111111111111111",
        PUMP_PROGRAM,
    ]
    keys = _keys((fee_payer, True), (user, True), *[(item, False) for item in accounts if item not in {fee_payer, user}])
    data = _b58encode(PUMP_CREATE_DISCRIMINATOR + b"\x00" * 8)
    return {
        "slot": 50,
        "blockTime": 1690000000,
        "transaction": {
            "signatures": [SIG_OLD],
            "message": {
                "accountKeys": keys,
                "instructions": [{"programId": PUMP_PROGRAM, "accounts": accounts, "data": data}],
            },
        },
        "meta": {"err": None, "innerInstructions": []},
    }


def _sigs(*signatures: str) -> list[dict]:
    return [{"signature": item, "slot": index + 1} for index, item in enumerate(signatures)]


def _history(script: RpcScript, signatures: list[str], transactions: dict) -> RpcScript:
    script.signatures = _sigs(*signatures)
    script.transactions = transactions
    return script


def test_classic_spl_initialize_mint() -> None:
    parsed = parse_mint_initialization_transaction(
        TOKEN_A,
        _init_tx(extra_signers=[INIT]),
        signature=SIG_OLD,
        observed_at=NOW,
    )
    assert parsed.inspection_status == STATUS_VERIFIED
    assert parsed.create_instruction == "initializeMint"
    assert parsed.create_program_id == TOKEN_PROGRAM
    assert parsed.token_program == TOKEN_PROGRAM
    assert parsed.mint_init_signer == INIT
    assert parsed.fee_payer == FEE
    assert parsed.mint_init_signer != parsed.fee_payer


def test_token_2022_initialize_mint2() -> None:
    parsed = parse_mint_initialization_transaction(
        TOKEN_A,
        _init_tx(program_id=TOKEN_2022_PROGRAM, ix_type="initializeMint2", extra_signers=[INIT]),
        signature=SIG_OLD,
        observed_at=NOW,
    )
    assert parsed.inspection_status == STATUS_VERIFIED
    assert parsed.create_instruction == "initializeMint2"
    assert parsed.token_program == TOKEN_2022_PROGRAM
    assert parsed.mint_init_signer == INIT


def test_pump_create_matches_requested_mint() -> None:
    parsed = parse_mint_initialization_transaction(TOKEN_A, _pump_tx(), signature=SIG_OLD, observed_at=NOW)
    assert parsed.inspection_status == STATUS_VERIFIED
    assert parsed.create_program_id == PUMP_PROGRAM
    assert parsed.create_instruction == "create"
    assert parsed.mint_init_signer == INIT
    assert parsed.fee_payer == FEE
    assert parsed.token_program == TOKEN_PROGRAM


def test_pump_create_does_not_match_other_mint() -> None:
    parsed = parse_mint_initialization_transaction(MINT, _pump_tx(mint=TOKEN_A), signature=SIG_OLD, observed_at=NOW)
    assert parsed.inspection_status == STATUS_UNKNOWN
    assert parsed.mint_init_signer is None
    assert parsed.create_instruction is None


def test_multiple_signers_are_ambiguous() -> None:
    parsed = parse_mint_initialization_transaction(
        TOKEN_A,
        _init_tx(extra_signers=[INIT, OTHER]),
        signature=SIG_OLD,
        observed_at=NOW,
    )
    assert parsed.inspection_status == STATUS_UNKNOWN
    assert parsed.mint_init_status == STATUS_UNKNOWN
    assert parsed.mint_init_signer is None
    assert INIT in parsed.signer_addresses
    assert OTHER in parsed.signer_addresses
    assert parsed.create_instruction == "initializeMint"
    assert "fee payer is not assumed" in (parsed.parser_notes or "").lower()


def test_fee_payer_is_not_assumed_creator() -> None:
    parsed = parse_mint_initialization_transaction(
        TOKEN_A,
        _init_tx(extra_signers=[], include_extra_in_ix=False),
        signature=SIG_OLD,
        observed_at=NOW,
    )
    assert parsed.fee_payer == FEE
    assert parsed.mint_init_signer is None
    assert parsed.mint_init_status == STATUS_UNKNOWN
    assert parsed.inspection_status == STATUS_UNKNOWN
    assert "not assumed" in (parsed.parser_notes or "").lower()


def test_missing_signature_history_is_unknown() -> None:
    script = _history(RpcScript(), [], {})

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.mint_init_status == STATUS_UNKNOWN
    assert result.observed_at is None
    assert "empty" in (result.parser_notes or "").lower()


def test_missing_transaction_is_unavailable_not_verified_absent() -> None:
    script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: None})

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.mint_init_status == STATUS_UNAVAILABLE
    assert result.mint_init_signer is None
    assert "unavailable" in (result.parser_notes or "").lower()
    assert "absent" in (result.parser_notes or "").lower()


def test_unsupported_instruction_is_unknown() -> None:
    script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: _transfer_tx(mint=MINT)})

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.create_instruction is None
    assert "supported" in (result.parser_notes or "").lower()


def test_rpc_timeout_is_unavailable() -> None:
    script = RpcScript()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    script.handler = boom  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.mint_init_status == STATUS_UNAVAILABLE


def test_rpc_429_is_unavailable() -> None:
    script = RpcScript()
    script._rate_remaining = 10
    script.signatures = _sigs(SIG_OLD)

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert any("Rate limited" in item or "429" in item for item in result.errors) or "RPC" in (result.parser_notes or "")


def test_malformed_transaction_is_unknown() -> None:
    script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: "not-an-object"})

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_UNKNOWN
    assert result.mint_init_status == STATUS_UNKNOWN


def test_oldest_relevant_transaction_is_selected() -> None:
    script = _history(
        RpcScript(),
        [SIG_NEW, SIG_OLD],
        {SIG_NEW: _transfer_tx(mint=MINT), SIG_OLD: _init_tx(mint=MINT, extra_signers=[INIT])},
    )

    async def body():
        async with script.client() as rpc:
            return await inspect_mint_initialization(MINT, rpc)

    result = run(body())
    assert result.inspection_status == STATUS_VERIFIED
    assert result.mint_init_signature == SIG_OLD
    assert result.mint_init_signer == INIT
    assert result.observed_at is not None
    assert script.calls[0] == "getSignaturesForAddress"
    assert script.calls[1] == "getTransaction"


def test_cache_preserves_observed_at() -> None:
    script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: _init_tx(mint=MINT, extra_signers=[INIT])})
    client = script.client()
    client._cache_ttl = 60

    async def body():
        async with client as rpc:
            first = await inspect_mint_initialization(MINT, rpc)
            second = await inspect_mint_initialization(MINT, rpc)
            return first, second

    first, second = run(body())
    assert first.observed_at is not None
    assert second.observed_at == first.observed_at
    assert second.from_cache is True


def test_historical_as_of_excludes_later_initialization() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        early = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status=STATUS_UNKNOWN,
            mint_init_status=STATUS_UNKNOWN,
            parser_notes="early unknown",
            observed_at=NOW,
            recorded_at=NOW,
            source="solana_rpc",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        later = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status=STATUS_VERIFIED,
            mint_init_status=STATUS_VERIFIED,
            mint_init_signer=INIT,
            fee_payer=FEE,
            parser_notes="later verified",
            observed_at=NOW + timedelta(hours=2),
            recorded_at=NOW + timedelta(hours=2),
            source="solana_rpc",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        session.add_all([early, later])
        session.commit()
        as_of = latest_observation_as_of(session, token.id, NOW + timedelta(minutes=30))
        assert as_of is not None
        assert as_of.inspection_status == STATUS_UNKNOWN
        assert as_of.mint_init_signer is None
        excluded = latest_observation_as_of(session, token.id, NOW - timedelta(minutes=1))
        assert excluded is None
        latest = latest_observation_as_of(session, token.id, NOW + timedelta(hours=3))
        assert latest is not None
        assert latest.inspection_status == STATUS_VERIFIED
    finally:
        session.close()


def test_append_only_observations_and_wallet_activity_untouched() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.execute(delete(WalletActivity))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        session.add(
            WalletActivity(
                token_id=token.id,
                deployer_address="Estimate111111111111111111111111111111111",
                deployer_status="ESTIMATE",
            )
        )
        session.commit()
        first = persist_creator_observation(
            session,
            token,
            parse_mint_initialization_transaction(
                MINT,
                _init_tx(mint=MINT, extra_signers=[INIT]),
                signature=SIG_OLD,
                observed_at=NOW,
            ),
            recorded_at=NOW,
        )
        second = persist_creator_observation(
            session,
            token,
            parse_mint_initialization_transaction(
                MINT,
                _init_tx(mint=MINT, extra_signers=[INIT]),
                signature=SIG_OLD,
                observed_at=NOW + timedelta(minutes=5),
            ),
            recorded_at=NOW + timedelta(minutes=5),
        )
        rows = list(session.scalars(select(CreatorObservation).order_by(CreatorObservation.id.asc())).all())
        assert len(rows) == 2
        assert rows[0].id == first.id
        assert as_utc(rows[0].observed_at) == NOW
        assert rows[1].id == second.id
        wallet = session.scalar(select(WalletActivity))
        assert wallet is not None
        assert wallet.deployer_address == "Estimate111111111111111111111111111111111"
        assert wallet.deployer_status == "ESTIMATE"
    finally:
        session.close()


def test_creator_scan_limit_and_api(client) -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.commit()
        session.add(Token(token_address=MINT, symbol="A", name="Alpha", chain="solana"))
        session.commit()
    finally:
        session.close()

    status = client.get("/api/v1/creator/status")
    assert status.status_code == 200
    assert status.json()["last_status"] in {"never", "completed", "running"}
    missing = client.get("/api/v1/creator/tokens/UnknownMint1111111111111111111111111111111")
    assert missing.status_code == 404
    listed = client.get(f"/api/v1/creator/tokens/{MINT}")
    assert listed.status_code == 200
    assert listed.json()["observation"] is None
    history = client.get(f"/api/v1/creator/tokens/{MINT}/history")
    assert history.status_code == 200
    assert history.json()["observations"] == []


def test_scan_serializes_rpc_and_persists(client) -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(CreatorObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="A", name="Alpha", chain="solana")
        session.add(token)
        session.commit()
        script = _history(RpcScript(), [SIG_OLD], {SIG_OLD: _init_tx(mint=MINT, extra_signers=[INIT])})

        async def body():
            async with script.client() as rpc:
                return await scan_creator_tokens(session, rpc, get_settings(), limit=5)

        summary = run(body())
        assert summary.tokens_scanned == 1
        assert summary.verified == 1
        assert summary.applied_limit == 5
        assert summary.max_scan_limit == CREATOR_SCAN_HARD_MAX
        assert script.calls == ["getSignaturesForAddress", "getTransaction"]
        row = session.scalar(select(CreatorObservation))
        assert row is not None
        assert row.mint_init_signer == INIT
        assert row.fee_payer == FEE
        assert row.inspection_status == STATUS_VERIFIED
        scan = session.scalar(select(IntelligenceScan).where(IntelligenceScan.scan_type == "creator"))
        assert scan is not None
    finally:
        session.close()

    payload = client.get(f"/api/v1/creator/tokens/{MINT}").json()
    observation = payload["observation"]
    assert observation["mint_init_signer"] == INIT
    assert observation["fee_payer"] == FEE
    assert "mint_init_signer" not in observation["unknown_fields"]


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


def test_sample_unknown_observation_fields() -> None:
    parsed = parse_mint_initialization_transaction(
        TOKEN_A,
        _init_tx(extra_signers=[], include_extra_in_ix=False),
        signature=SIG_OLD,
        observed_at=NOW,
    )
    assert parsed.inspection_status == STATUS_UNKNOWN
    assert parsed.mint_init_signer is None
    assert parsed.mint_init_status == STATUS_UNKNOWN
    assert parsed.fee_payer == FEE
    assert parsed.create_instruction == "initializeMint"
    unknown = [
        name
        for name, value in (
            ("mint_init_signer", parsed.mint_init_signer),
            ("mint_init_status", None if parsed.mint_init_status != STATUS_VERIFIED else parsed.mint_init_status),
        )
        if value is None
    ]
    assert "mint_init_signer" in unknown
