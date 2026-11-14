"""Phase 9B Metaplex metadata, provenance, and append-only security tests."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    HistoricalEvaluation,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    PointInTimeRun,
    Token,
    TokenRiskAssessment,
    TokenSecurityScan,
)
from app.services.metaplex_metadata import (
    AVAILABLE,
    MALFORMED,
    METADATA_PROGRAM_ID,
    NOT_FOUND,
    TOKEN_2022_PROGRAM,
    UNAVAILABLE,
    UNSUPPORTED,
    MetadataDecodeError,
    decode_metadata_account,
    derive_metadata_pda,
    encode_metadata_account,
    fetch_metaplex_metadata,
)
from app.services.security_scan import scan_tokens
from app.services.solana_pda import PdaError, b58decode, b58encode
from app.services.solana_rpc import SolanaRpcClient
from app.services.token_security import analyze_token_security
from app.services.opportunity_scoring import observation_time, observations_as_of
from tests.dexscreener_fakes import run
from tests.test_security import PAIR, RpcScript, _clean, mint_account


MINT_BYTES = bytes(range(32))
AUTH_BYTES = bytes(range(32, 64))
MINT = b58encode(MINT_BYTES)


def _metadata_account(raw: bytes) -> dict:
    return {
        "owner": METADATA_PROGRAM_ID,
        "data": [base64.b64encode(raw).decode(), "base64"],
        "lamports": 1,
    }


def _encoded(*, mutable: bool = True) -> bytes:
    return encode_metadata_account(
        update_authority=AUTH_BYTES,
        mint=MINT_BYTES,
        name="Doge",
        symbol="DOGE",
        uri="https://example.com/meta.json",
        is_mutable=mutable,
    )


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_METADATA_PDA = "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq"


def test_pda_derivation_is_deterministic() -> None:
    first = derive_metadata_pda(MINT)
    second = derive_metadata_pda(MINT)
    assert first == second
    assert first != MINT
    other = derive_metadata_pda(b58encode(bytes(range(1, 33))))
    assert other != first


def test_usdc_metadata_pda_matches_mainnet_vector() -> None:
    assert derive_metadata_pda(USDC_MINT) == USDC_METADATA_PDA


def test_b58decode_rejects_short_overlong_and_invalid_keys() -> None:
    for value in ("abc", "TokenA", "0OIl", ""):
        try:
            b58decode(value)
            raise AssertionError(f"expected PdaError for {value!r}")
        except PdaError:
            pass
    assert len(b58decode(USDC_MINT)) == 32
    assert b58decode("1" * 32) == bytes(32)


def test_valid_metaplex_metadata_decoding() -> None:
    decoded = decode_metadata_account(_encoded(mutable=False))
    assert decoded["name"] == "Doge"
    assert decoded["symbol"] == "DOGE"
    assert decoded["uri"] == "https://example.com/meta.json"
    assert decoded["is_mutable"] is False
    assert decoded["update_authority"] == b58encode(AUTH_BYTES)


def test_fetch_available_metadata_preserves_slot_and_timestamp() -> None:
    script = RpcScript()
    script.account = mint_account()
    script.metadata = _metadata_account(_encoded(mutable=True))

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.metadata_status == AVAILABLE
    assert result.metadata_name == "Doge"
    assert result.metadata_symbol == "DOGE"
    assert result.metadata_uri == "https://example.com/meta.json"
    assert result.metadata_is_mutable is True
    assert result.metadata_mutability == "MUTABLE"
    assert result.metadata_account == derive_metadata_pda(MINT)
    assert result.slot == 4242
    assert result.observed_at is not None
    assert result.source == "solana_rpc"
    assert "not automatically a scam" in " ".join(result.findings)


def test_missing_metadata_account_is_not_found() -> None:
    script = RpcScript()
    script.account = mint_account()
    script.metadata = None

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.available is True
    assert result.metadata_status == NOT_FOUND
    assert result.metadata_name is None
    assert result.metadata_is_mutable is None
    assert result.metadata_mutability == "UNKNOWN"
    assert any("not malicious" in item.lower() for item in result.findings)


def test_malformed_metadata_does_not_invent_fields() -> None:
    script = RpcScript()
    script.account = mint_account()
    script.metadata = _metadata_account(b"\x04\x00\x01")

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    result = run(body())
    assert result.status == MALFORMED
    assert result.name is None
    assert result.symbol is None
    assert result.uri is None
    assert result.is_mutable is None
    assert result.update_authority is None


def test_token_2022_without_pda_is_unsupported() -> None:
    script = RpcScript()
    account = mint_account()
    account["owner"] = TOKEN_2022_PROGRAM
    script.account = account
    script.metadata = None

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(MINT, rpc)

    result = run(body())
    assert result.token_standard == "token-2022"
    assert result.metadata_status == UNSUPPORTED
    assert result.metadata_name is None


def test_rpc_timeout_and_unavailable_metadata() -> None:
    script = RpcScript()
    script.account = mint_account()
    script.metadata = httpx.Response(500, json={"error": "down"})

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    unavailable = run(body())
    assert unavailable.status == UNAVAILABLE
    assert unavailable.name is None

    timed = RpcScript()
    timed.account = mint_account()

    def timeout_handler(request: httpx.Request):
        payload = request.content.decode()
        if "base64" in payload:
            raise httpx.TimeoutException("rpc timeout")
        return RpcScript.handler(timed, request)

    timed.handler = timeout_handler  # type: ignore[method-assign]

    async def timeout_body():
        async with timed.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    timed_out = run(timeout_body())
    assert timed_out.status == UNAVAILABLE


def test_append_only_security_rows_and_null_liquidity() -> None:
    session = get_session_factory()()
    _clean(session)
    eval_row = HistoricalEvaluation(horizon="next_observation", explanation="keep")
    pit_row = PointInTimeRun(status="completed", candidates_count=17197, summary_json='{"keep":true}')
    session.add_all([eval_row, pit_row])
    token = Token(token_address=MINT, name="Test", symbol="TST", chain="solana", pair_address=PAIR)
    session.add(token)
    session.commit()
    eval_id, eval_text = eval_row.id, eval_row.explanation
    pit_id, pit_json = pit_row.id, pit_row.summary_json
    script = RpcScript()
    script.account = mint_account()
    script.largest = []

    async def body():
        async with script.client() as rpc:
            await scan_tokens(session, rpc, token_address=MINT, force=True)
            await scan_tokens(session, rpc, token_address=MINT, force=True)

    run(body())
    rows = list(session.scalars(select(TokenSecurityScan).order_by(TokenSecurityScan.id.asc())).all())
    assert len(rows) == 2
    first, second = rows
    assert first.id != second.id
    assert first.metadata_status == NOT_FOUND
    assert first.slot == 4242
    assert first.source == "solana_rpc"
    assert first.observed_at is not None
    assert first.recorded_at is not None
    assert first.recorded_at >= first.observed_at
    first_observed = first.observed_at
    session.refresh(first)
    assert first.observed_at == first_observed
    risks = list(session.scalars(select(TokenRiskAssessment)).all())
    assert len(risks) == 2
    liquidity = list(session.scalars(select(LiquidityAnalysisRecord)).all())
    assert len(liquidity) == 2
    assert all(item.liquidity_usd is None for item in liquidity)
    snapshots = list(session.scalars(select(MarketSnapshot)).all())
    assert snapshots == []
    kept_eval = session.get(HistoricalEvaluation, eval_id)
    kept_pit = session.get(PointInTimeRun, pit_id)
    assert kept_eval is not None and kept_eval.explanation == eval_text
    assert kept_pit is not None and kept_pit.summary_json == pit_json
    assert get_settings().security_scan_limit == 25
    session.execute(delete(HistoricalEvaluation).where(HistoricalEvaluation.id == eval_id))
    session.execute(delete(PointInTimeRun).where(PointInTimeRun.id == pit_id))
    session.commit()
    session.close()


def test_correct_metaplex_owner_is_available() -> None:
    script = RpcScript()
    script.metadata = _metadata_account(_encoded())

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    result = run(body())
    assert result.status == AVAILABLE
    assert result.name == "Doge"


def test_wrong_owner_is_not_available() -> None:
    script = RpcScript()
    account = _metadata_account(_encoded())
    account["owner"] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    script.metadata = account

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    result = run(body())
    assert result.status == MALFORMED
    assert result.name is None
    assert result.symbol is None
    assert result.uri is None
    assert any("not owned by the Metaplex" in item for item in result.findings)


def test_missing_owner_is_not_available() -> None:
    script = RpcScript()
    account = _metadata_account(_encoded())
    del account["owner"]
    script.metadata = account

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    result = run(body())
    assert result.status == MALFORMED
    assert result.name is None
    assert "owner is missing" in " ".join(result.errors)


def test_invalid_creators_option_is_malformed() -> None:
    raw = bytearray(_encoded())
    raw[-3] = 2
    with pytest.raises(MetadataDecodeError, match="Invalid creators option"):
        decode_metadata_account(bytes(raw))


def test_invalid_utf8_is_malformed() -> None:
    def borsh(text: str) -> bytes:
        raw = text.encode("utf-8")
        return len(raw).to_bytes(4, "little") + raw

    payload = bytearray()
    payload.append(4)
    payload.extend(AUTH_BYTES)
    payload.extend(MINT_BYTES)
    payload.extend((3).to_bytes(4, "little") + b"\xff\xfe\xfd")
    payload.extend(borsh("DOGE"))
    payload.extend(borsh("https://example.com/meta.json"))
    payload.extend((0).to_bytes(2, "little"))
    payload.append(0)
    payload.append(0)
    payload.append(1)
    with pytest.raises(MetadataDecodeError, match="UTF-8"):
        decode_metadata_account(bytes(payload))


def test_rpc_observed_at_is_after_successful_response_and_cache_keeps_provenance() -> None:
    script = RpcScript()
    script.account = mint_account()
    before = datetime.now(timezone.utc)

    async def body():
        async with SolanaRpcClient(
            settings=get_settings(),
            transport=httpx.MockTransport(script.handler),
            cache_ttl_seconds=60,
        ) as rpc:
            first = await rpc.get_account_info(MINT, encoding="jsonParsed")
            after_first = datetime.now(timezone.utc)
            second = await rpc.get_account_info(MINT, encoding="jsonParsed")
            return first, second, after_first, script.calls.count("getAccountInfo")

    first, second, after_first, calls = run(body())
    assert first.from_cache is False
    assert first.observed_at is not None
    assert first.observed_at >= before
    assert first.observed_at <= after_first
    assert first.slot == 4242
    assert second.from_cache is True
    assert second.observed_at == first.observed_at
    assert second.slot == first.slot
    assert calls == 1


def test_rpc_failure_does_not_invent_observed_at() -> None:
    script = RpcScript()
    script.metadata = httpx.Response(500, json={"error": "down"})

    async def body():
        async with script.client() as rpc:
            return await fetch_metaplex_metadata(MINT, rpc)

    result = run(body())
    assert result.status == UNAVAILABLE
    assert result.observed_at is None


def test_recorded_at_is_not_used_as_observation_time() -> None:
    t = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    later = TokenRiskAssessment(
        token_id=1,
        scanned_at=t,
        observed_at=t + timedelta(minutes=5),
        recorded_at=t + timedelta(minutes=6),
        status="scored",
        risk_score=90,
        explanation="later",
    )
    assert observation_time(later) == t + timedelta(minutes=5)
    assert observations_as_of([later], t) == []
    early = TokenRiskAssessment(
        token_id=1,
        scanned_at=t + timedelta(minutes=5),
        observed_at=t,
        recorded_at=t + timedelta(minutes=6),
        status="scored",
        risk_score=90,
        explanation="early",
    )
    assert observations_as_of([early], t)
    legacy = TokenRiskAssessment(
        token_id=1,
        scanned_at=t,
        observed_at=None,
        recorded_at=t + timedelta(minutes=6),
        status="scored",
        risk_score=90,
        explanation="legacy",
    )
    assert observation_time(legacy) == t
    assert observations_as_of([legacy], t)
