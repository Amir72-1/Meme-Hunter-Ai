"""Phase 9C.1 Token-2022 extension parsing, provenance, and PIT tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    HistoricalEvaluation,
    MarketSnapshot,
    PointInTimeRun,
    Token,
    Token2022ExtensionObservation,
    TokenRiskAssessment,
    TokenSecurityScan,
)
from app.services.security_scan import scan_tokens
from app.services.solana_rpc import RpcAccountResult, SolanaRpcClient
from app.services.token_2022_extensions import (
    STATUS_AVAILABLE,
    STATUS_NO_EXTENSIONS,
    STATUS_UNAVAILABLE,
    STATUS_UNSUPPORTED,
    inspect_token_2022_mint,
    latest_observation_as_of,
)
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, analyze_token_security
from tests.dexscreener_fakes import run
from tests.test_metaplex_metadata import MINT, _encoded, _metadata_account
from tests.test_security import PAIR, RpcScript, _clean, mint_account

HOOK = "Hook111111111111111111111111111111111111111"
DELEGATE = "PrmD111111111111111111111111111111111111111"
CLOSE = "Close11111111111111111111111111111111111111"
META_PTR = "MetaP11111111111111111111111111111111111111"


def _t22_account(extensions=None, *, include_extensions: bool = True, program: str = "spl-token-2022") -> dict:
    info = {
        "mintAuthority": MINT,
        "freezeAuthority": None,
        "supply": "1000000",
        "decimals": 6,
        "isInitialized": True,
    }
    if include_extensions:
        info["extensions"] = extensions if extensions is not None else []
    return {
        "owner": TOKEN_2022_PROGRAM,
        "data": {
            "program": program,
            "parsed": {"type": "mint", "info": info},
            "space": 400,
        },
    }


def _multi_extension_list() -> list[dict]:
    return [
        {
            "extension": "transferFeeConfig",
            "state": {
                "newerTransferFee": {"epoch": 0, "maximumFee": "1000000", "transferFeeBasisPoints": 150},
                "olderTransferFee": {"epoch": 0, "maximumFee": "1000000", "transferFeeBasisPoints": 150},
                "transferFeeConfigAuthority": MINT,
                "withdrawWithheldAuthority": CLOSE,
            },
        },
        {"extension": "transferHook", "state": {"authority": MINT, "programId": HOOK}},
        {"extension": "permanentDelegate", "state": {"delegate": DELEGATE}},
        {"extension": "defaultAccountState", "state": {"accountState": "initialized"}},
        {"extension": "nonTransferable"},
        {"extension": "pausable", "state": {"authority": MINT, "paused": False}},
        {"extension": "mintCloseAuthority", "state": {"closeAuthority": CLOSE}},
        {
            "extension": "confidentialTransferMint",
            "state": {"authority": MINT, "autoApproveNewAccounts": False, "auditorElgamalPubkey": None},
        },
        {"extension": "metadataPointer", "state": {"authority": MINT, "metadataAddress": META_PTR}},
        {
            "extension": "tokenMetadata",
            "state": {"additionalMetadata": [], "mint": MINT, "name": "Demo", "symbol": "DEMO", "updateAuthority": MINT, "uri": "https://example.com"},
        },
        {"extension": "scaledUiAmount", "state": {"authority": MINT, "multiplier": "1.5"}},
        {"extension": "interestBearingConfig", "state": {"currentRate": 0, "preUpdateAverageRate": 0}},
    ]


def test_parsed_token_2022_mint_with_multiple_extensions() -> None:
    account = _t22_account(_multi_extension_list())
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=account, slot=9001, observed_at=datetime.now(timezone.utc)),
    )
    assert result.inspection_status == STATUS_AVAILABLE
    assert result.parser_complete is True
    assert result.transfer_fee_bps == 150
    assert result.transfer_fee_max == "1000000"
    assert result.transfer_fee_config_authority == MINT
    assert result.transfer_fee_withdraw_authority == CLOSE
    assert result.transfer_hook_program_id == HOOK
    assert result.transfer_hook_authority == MINT
    assert result.permanent_delegate == DELEGATE
    assert result.default_account_state == "initialized"
    assert result.pausable_paused is False
    assert result.pausable_authority == MINT
    assert result.mint_close_authority == CLOSE
    assert result.confidential_transfer_present is True
    assert result.confidential_auto_approve is False
    assert result.metadata_pointer_address == META_PTR
    assert result.token_metadata_present is True
    assert result.scaled_ui_amount_multiplier == "1.5"
    assert "interestBearingConfig" in result.extension_types
    assert "transferFeeConfig" in result.extension_types
    assert any("not a scam" in item.lower() or "evidence only" in item.lower() or "malicious" in item.lower() for item in result.findings)


def test_verified_empty_extension_list_is_no_extensions() -> None:
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=_t22_account([]), slot=1, observed_at=datetime.now(timezone.utc)),
    )
    assert result.inspection_status == STATUS_NO_EXTENSIONS
    assert result.parser_complete is True
    assert result.extension_types == []


def test_omitted_extensions_field_is_unavailable_not_empty() -> None:
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=_t22_account(include_extensions=False), slot=1, observed_at=datetime.now(timezone.utc)),
    )
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.parser_complete is False
    assert result.observed_at is not None


def test_unknown_extension_type_is_preserved() -> None:
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(
            value=_t22_account([{"extension": "futureControlWidget", "state": {"flag": True}}]),
            slot=2,
            observed_at=datetime.now(timezone.utc),
        ),
    )
    assert result.inspection_status == STATUS_AVAILABLE
    assert result.extension_types == ["futureControlWidget"]
    assert result.parsed_fields["futureControlWidget"]["flag"] is True


def test_unsupported_binary_representation() -> None:
    account = {
        "owner": TOKEN_2022_PROGRAM,
        "data": ["AQIDBA==", "base64"],
    }
    result = inspect_token_2022_mint(MINT, RpcAccountResult(value=account, slot=3, observed_at=datetime.now(timezone.utc)))
    assert result.inspection_status == STATUS_UNSUPPORTED
    assert result.parser_complete is False


def test_malformed_rpc_account_is_unavailable() -> None:
    result = inspect_token_2022_mint(MINT, RpcAccountResult(value={"owner": TOKEN_2022_PROGRAM, "data": 12}, slot=4))
    assert result.inspection_status == STATUS_UNAVAILABLE
    result2 = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=_t22_account(["not-an-object"]), slot=4, observed_at=datetime.now(timezone.utc)),
    )
    assert result2.inspection_status == STATUS_UNAVAILABLE
    assert result2.parser_complete is False


def test_rpc_failure_does_not_set_observed_at() -> None:
    result = inspect_token_2022_mint(MINT, None, rpc_error="timeout")
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.observed_at is None
    assert result.slot is None


def test_missing_context_slot_is_preserved_as_none() -> None:
    observed = datetime.now(timezone.utc)
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=_t22_account([]), slot=None, observed_at=observed),
    )
    assert result.slot is None
    assert result.observed_at == observed
    assert result.inspection_status == STATUS_NO_EXTENSIONS


def test_unparseable_extension_is_incomplete() -> None:
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(
            value=_t22_account(
                [
                    {"extension": "transferHook", "state": {"programId": HOOK}},
                    {"extension": "unparseableExtension"},
                ]
            ),
            slot=5,
            observed_at=datetime.now(timezone.utc),
        ),
    )
    assert result.inspection_status == STATUS_UNAVAILABLE
    assert result.parser_complete is False
    assert "transferHook" in result.extension_types
    assert "unparseableExtension" in result.extension_types


def test_cache_hit_preserves_observed_at_and_slot() -> None:
    script = RpcScript()
    script.account = _t22_account(_multi_extension_list())

    async def body():
        async with SolanaRpcClient(
            settings=get_settings(),
            transport=httpx.MockTransport(script.handler),
            cache_ttl_seconds=60,
        ) as rpc:
            first = await analyze_token_security(MINT, rpc)
            second = await analyze_token_security(MINT, rpc)
            return first, second, script.calls.count("getAccountInfo")

    first, second, calls = run(body())
    assert first.token_2022_inspection.from_cache is False
    assert second.token_2022_inspection.from_cache is True
    assert second.observed_at == first.observed_at
    assert second.slot == first.slot
    assert first.token_2022_inspection.slot == 4242
    assert second.token_2022_inspection.observed_at == first.token_2022_inspection.observed_at
    assert second.token_2022_inspection.slot == first.token_2022_inspection.slot
    assert calls == 2


def test_rpc_failure_through_analyze_token_security() -> None:
    script = RpcScript()
    script.account = httpx.Response(500, json={"error": "down"})

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.available is False
    assert result.observed_at is None
    assert result.token_2022_inspection.inspection_status == STATUS_UNAVAILABLE
    assert result.token_2022_inspection.observed_at is None


def test_recorded_at_not_before_observed_at_and_append_only() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=MINT, name="T22", symbol="T22", chain="solana", pair_address=PAIR)
    session.add(token)
    session.commit()
    script = RpcScript()
    script.account = _t22_account(_multi_extension_list())
    script.largest = []

    async def body():
        async with script.client() as rpc:
            await scan_tokens(session, rpc, token_address=MINT, force=True)
            await scan_tokens(session, rpc, token_address=MINT, force=True)

    run(body())
    rows = list(session.scalars(select(Token2022ExtensionObservation).order_by(Token2022ExtensionObservation.id.asc())).all())
    assert len(rows) == 2
    assert rows[0].id != rows[1].id
    assert rows[0].inspection_status == STATUS_AVAILABLE
    assert rows[0].observed_at is not None
    assert rows[0].recorded_at >= rows[0].observed_at
    assert rows[1].recorded_at >= rows[1].observed_at
    first_observed, first_slot = rows[0].observed_at, rows[0].slot
    session.refresh(rows[0])
    assert rows[0].observed_at == first_observed
    assert rows[0].slot == first_slot
    session.close()


def test_pit_excludes_later_observations() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=MINT + "pit", name="PIT", symbol="PIT", chain="solana")
    session.add(token)
    session.commit()
    t0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    early = Token2022ExtensionObservation(
        token_id=token.id,
        mint_address=token.token_address,
        token_program=TOKEN_2022_PROGRAM,
        inspection_status=STATUS_NO_EXTENSIONS,
        extension_types_json="[]",
        parsed_fields_json="{}",
        parser_complete=1,
        source="solana_rpc",
        observed_at=t0,
        recorded_at=t0 + timedelta(seconds=1),
        slot=10,
    )
    later = Token2022ExtensionObservation(
        token_id=token.id,
        mint_address=token.token_address,
        token_program=TOKEN_2022_PROGRAM,
        inspection_status=STATUS_AVAILABLE,
        extension_types_json='["transferHook"]',
        parsed_fields_json="{}",
        parser_complete=1,
        source="solana_rpc",
        observed_at=t0 + timedelta(hours=2),
        recorded_at=t0 + timedelta(hours=2, seconds=1),
        slot=99,
    )
    session.add_all([early, later])
    session.commit()
    as_of = latest_observation_as_of(session, token.id, t0 + timedelta(minutes=30))
    assert as_of is not None
    assert as_of.id == early.id
    assert as_of.inspection_status == STATUS_NO_EXTENSIONS
    too_early = latest_observation_as_of(session, token.id, t0 - timedelta(seconds=1))
    assert too_early is None
    session.close()


def test_legacy_security_rows_remain_unchanged() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=MINT + "legacy", name="Legacy", symbol="LEG", chain="solana", pair_address=PAIR)
    session.add(token)
    session.commit()
    scanned = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy = TokenSecurityScan(
        token_id=token.id,
        scanned_at=scanned,
        source="solana_rpc",
        observed_at=scanned,
        recorded_at=scanned,
        slot=7,
        mint_authority_status="VERIFIED_PRESENT",
        freeze_authority_status="UNKNOWN",
        token_standard="spl-token",
        findings_json="[]",
        errors_json="[]",
    )
    session.add(legacy)
    session.commit()
    legacy_id = legacy.id
    legacy_slot = legacy.slot
    script = RpcScript()
    script.account = _t22_account([])
    script.largest = []

    async def body():
        async with script.client() as rpc:
            await scan_tokens(session, rpc, token_address=token.token_address, force=True)

    run(body())
    kept = session.get(TokenSecurityScan, legacy_id)
    assert kept is not None
    assert kept.slot == legacy_slot
    assert kept.token_standard == "spl-token"
    assert kept.observed_at == scanned
    scans = list(session.scalars(select(TokenSecurityScan).where(TokenSecurityScan.token_id == token.id)).all())
    assert len(scans) == 2
    session.close()


def test_standard_spl_token_is_unsupported_for_extensions() -> None:
    result = inspect_token_2022_mint(
        MINT,
        RpcAccountResult(value=mint_account(), slot=8, observed_at=datetime.now(timezone.utc)),
    )
    assert result.inspection_status == STATUS_UNSUPPORTED
    assert result.token_program == TOKEN_PROGRAM
    assert result.parser_complete is True

    script = RpcScript()
    script.account = mint_account()

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    security = run(body())
    assert security.token_standard == "spl-token"
    assert security.mint_authority_status == "VERIFIED_PRESENT"
    assert security.token_2022_inspection.inspection_status == STATUS_UNSUPPORTED


def test_metaplex_metadata_still_available_with_token_2022_extensions() -> None:
    script = RpcScript()
    script.account = _t22_account(_multi_extension_list())
    script.metadata = _metadata_account(_encoded())

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.token_standard == "token-2022"
    assert result.token_2022_inspection.inspection_status == STATUS_AVAILABLE
    assert result.metadata_status == "AVAILABLE"
    assert result.metadata_name == "Doge"


def test_snapshot_and_historical_evaluation_immutability() -> None:
    session = get_session_factory()()
    _clean(session)
    eval_row = HistoricalEvaluation(horizon="next_observation", explanation="phase9c1-keep")
    pit_row = PointInTimeRun(status="completed", candidates_count=3, summary_json='{"keep":true}')
    token = Token(token_address=MINT, name="Snap", symbol="SNP", chain="solana", pair_address=PAIR)
    session.add_all([eval_row, pit_row, token])
    session.commit()
    snap = MarketSnapshot(
        token_id=token.id,
        recorded_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        price_usd=1.25,
        liquidity_usd=5000.0,
    )
    session.add(snap)
    session.commit()
    eval_id, eval_text = eval_row.id, eval_row.explanation
    pit_id, pit_json = pit_row.id, pit_row.summary_json
    snap_id, snap_liq, snap_price = snap.id, snap.liquidity_usd, snap.price_usd
    script = RpcScript()
    script.account = _t22_account([])
    script.largest = []

    async def body():
        async with script.client() as rpc:
            await scan_tokens(session, rpc, token_address=MINT, force=True)

    run(body())
    kept_snap = session.get(MarketSnapshot, snap_id)
    assert kept_snap is not None
    assert kept_snap.liquidity_usd == snap_liq
    assert kept_snap.price_usd == snap_price
    kept_eval = session.get(HistoricalEvaluation, eval_id)
    kept_pit = session.get(PointInTimeRun, pit_id)
    assert kept_eval is not None and kept_eval.explanation == eval_text
    assert kept_pit is not None and kept_pit.summary_json == pit_json
    obs = list(session.scalars(select(Token2022ExtensionObservation)).all())
    assert len(obs) == 1
    assert obs[0].inspection_status == STATUS_NO_EXTENSIONS
    session.execute(delete(HistoricalEvaluation).where(HistoricalEvaluation.id == eval_id))
    session.execute(delete(PointInTimeRun).where(PointInTimeRun.id == pit_id))
    session.commit()
    session.close()


def test_malformed_getaccountinfo_raises_without_success_timestamp() -> None:
    script = RpcScript()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "nope"})

    async def body():
        async with SolanaRpcClient(
            settings=get_settings(),
            transport=httpx.MockTransport(handler),
            cache_ttl_seconds=0,
        ) as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.observed_at is None
    assert result.token_2022_inspection.observed_at is None
    assert result.token_2022_inspection.inspection_status == STATUS_UNAVAILABLE
