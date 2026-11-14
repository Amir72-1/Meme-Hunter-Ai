"""Phase 9C.4D Metaplex creators[] evidence tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    CreatorObservation,
    IntelligenceScan,
    MetaplexCreatorObservation,
    Token,
    TokenAccountOwnerObservation,
)
from app.services.metaplex_creator_scan import scan_metaplex_creators
from app.services.metaplex_creators import (
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    STATUS_VERIFIED_ABSENT,
    inspect_metaplex_creators,
    latest_observation_as_of,
)
from app.services.metaplex_metadata import (
    MALFORMED,
    METADATA_PROGRAM_ID,
    MetadataDecodeError,
    decode_metadata_account,
    encode_metadata_account,
    require_creator_address,
)
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from app.services.solana_pda import b58encode
from app.timeutil import as_utc
from tests.dexscreener_fakes import run
from tests.test_metaplex_metadata import AUTH_BYTES, MINT, MINT_BYTES, _encoded, _metadata_account
from tests.test_security import RpcScript, _clean

C1 = bytes(range(64, 96))
C2 = bytes(range(96, 128))
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)


def _creators_encoded(entries: list[tuple[bytes, int, int]] | None, *, option: int | None = None) -> bytes:
    return encode_metadata_account(
        update_authority=AUTH_BYTES,
        mint=MINT_BYTES,
        name="Doge",
        symbol="DOGE",
        uri="https://example.com/meta.json",
        is_mutable=True,
        creators=entries,
        creators_option=option,
    )


def _script_for(raw: bytes | None, *, owner: str = METADATA_PROGRAM_ID) -> RpcScript:
    script = RpcScript()
    if raw is None:
        script.metadata = None
    else:
        account = _metadata_account(raw)
        account["owner"] = owner
        script.metadata = account
    return script


def test_existing_metadata_decode_still_skips_creators_safely() -> None:
    decoded = decode_metadata_account(_encoded(mutable=False))
    assert decoded["name"] == "Doge"
    assert decoded["mint"] == MINT


def test_multiple_creators_verified_and_unverified() -> None:
    script = _script_for(_creators_encoded([(C1, 1, 60), (C2, 0, 40)]))

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_VERIFIED
    assert result.metadata_status == "AVAILABLE"
    assert result.creator_count == 2
    assert result.verified_creator_count == 1
    assert result.creators is not None
    assert result.creators[0]["address"] == b58encode(C1)
    assert result.creators[0]["verified"] is True
    assert result.creators[0]["share"] == 60
    assert result.creators[0]["order"] == 0
    assert result.creators[1]["verified"] is False
    assert result.creators[1]["order"] == 1
    assert result.observed_at is not None
    assert script.calls == ["getAccountInfo"]


def test_empty_creator_vector_is_verified_absent() -> None:
    script = _script_for(_creators_encoded([], option=1))

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_VERIFIED_ABSENT
    assert result.creators == []
    assert result.creator_count == 0


def test_explicitly_absent_creator_option_is_verified_absent() -> None:
    script = _script_for(_creators_encoded(None, option=0))

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_VERIFIED_ABSENT
    assert result.creators is None
    assert "explicitly absent" in (result.parser_notes or "").lower()


def test_null_rpc_account_is_unavailable_not_verified_absent() -> None:
    script = _script_for(None)

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNAVAILABLE
    assert result.creator_vector_status != STATUS_VERIFIED_ABSENT
    assert result.observed_at is None


def test_rpc_timeout_is_unavailable() -> None:
    script = RpcScript()

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    script.handler = boom  # type: ignore[method-assign]

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNAVAILABLE
    assert result.metadata_status == "UNAVAILABLE"


def test_rpc_429_is_unavailable() -> None:
    script = _script_for(_creators_encoded([(C1, 1, 100)]))
    script._rate_remaining = 10

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNAVAILABLE


def test_wrong_account_owner() -> None:
    script = _script_for(_creators_encoded([(C1, 1, 100)]), owner="11111111111111111111111111111111")

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.metadata_status == MALFORMED
    assert result.creator_vector_status == STATUS_UNKNOWN
    assert result.creators is None


def test_mint_mismatch() -> None:
    other_mint = bytes(range(1, 33))
    raw = encode_metadata_account(
        update_authority=AUTH_BYTES,
        mint=other_mint,
        name="X",
        symbol="X",
        uri="https://example.com/x.json",
        is_mutable=True,
        creators=[(C1, 1, 100)],
    )
    script = _script_for(raw)

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNKNOWN
    assert "does not match" in result.errors[0]


def test_invalid_creator_address() -> None:
    with pytest.raises(MetadataDecodeError, match="valid public key"):
        require_creator_address("not-a-pubkey")
    with pytest.raises(MetadataDecodeError, match="valid public key"):
        require_creator_address("")


def test_invalid_verified_flag() -> None:
    script = _script_for(_creators_encoded([(C1, 2, 100)]))

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNKNOWN
    assert result.creators is None
    assert any("verified" in item.lower() for item in result.errors)


def test_invalid_share_values() -> None:
    too_high = _script_for(_creators_encoded([(C1, 1, 150)]))
    unbalanced = _script_for(_creators_encoded([(C1, 1, 60), (C2, 0, 60)]))

    async def high():
        async with too_high.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    async def bad_sum():
        async with unbalanced.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    high_result = run(high())
    sum_result = run(bad_sum())
    assert high_result.creator_vector_status == STATUS_UNKNOWN
    assert sum_result.creator_vector_status == STATUS_UNKNOWN
    assert high_result.creators is None
    assert sum_result.creators is None


def test_truncated_vector() -> None:
    raw = _creators_encoded([(C1, 1, 60), (C2, 0, 40)])
    script = _script_for(raw[:-10])

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNKNOWN
    assert result.creators is None
    assert any("truncated" in item.lower() for item in result.errors)


def test_malformed_account_data() -> None:
    script = RpcScript()
    script.metadata = {"owner": METADATA_PROGRAM_ID, "data": ["%%%not-base64%%%", "base64"]}

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creator_vector_status == STATUS_UNKNOWN
    assert result.metadata_status == MALFORMED


def test_cache_preserves_observed_at() -> None:
    script = _script_for(_creators_encoded([(C1, 1, 100)]))
    client = script.client()
    client._cache_ttl = 60

    async def body():
        async with client as rpc:
            first = await inspect_metaplex_creators(MINT, rpc)
            second = await inspect_metaplex_creators(MINT, rpc)
            return first, second

    first, second = run(body())
    assert first.observed_at is not None
    assert second.observed_at == first.observed_at
    assert second.from_cache is True


def test_append_only_and_point_in_time() -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(MetaplexCreatorObservation))
        session.execute(delete(CreatorObservation))
        session.execute(delete(TokenAccountOwnerObservation))
        session.commit()
        token = Token(token_address=MINT, symbol="D", name="Doge", chain="solana")
        session.add(token)
        session.commit()
        creator_init = CreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            inspection_status="UNKNOWN",
            mint_init_status="UNKNOWN",
            source="solana_rpc",
            parser_notes="preexisting init",
            errors_json="[]",
            signer_addresses_json="[]",
        )
        holder = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=MINT,
            owner_classification="UNKNOWN",
            classification_status="UNKNOWN",
            source="solana_rpc",
            errors_json="[]",
        )
        session.add_all([creator_init, holder])
        session.commit()
        init_id, holder_id = creator_init.id, holder.id
        early = MetaplexCreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            metadata_status="AVAILABLE",
            creator_vector_status=STATUS_VERIFIED_ABSENT,
            creators_json=None,
            creator_count=0,
            observed_at=NOW,
            recorded_at=NOW,
            source="solana_rpc",
            parser_notes="early absent",
            errors_json="[]",
        )
        later = MetaplexCreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            metadata_status="AVAILABLE",
            creator_vector_status=STATUS_VERIFIED,
            creators_json='[{"address":"x","verified":true,"share":100,"order":0}]',
            creator_count=1,
            verified_creator_count=1,
            observed_at=NOW + timedelta(hours=2),
            recorded_at=NOW + timedelta(hours=2),
            source="solana_rpc",
            parser_notes="later verified",
            errors_json="[]",
        )
        null_time = MetaplexCreatorObservation(
            token_id=token.id,
            mint_address=MINT,
            metadata_status="UNAVAILABLE",
            creator_vector_status=STATUS_UNAVAILABLE,
            observed_at=None,
            recorded_at=NOW,
            source="solana_rpc",
            errors_json="[]",
        )
        session.add_all([early, later, null_time])
        session.commit()
        as_of = latest_observation_as_of(session, token.id, NOW + timedelta(minutes=30))
        assert as_of is not None
        assert as_of.creator_vector_status == STATUS_VERIFIED_ABSENT
        assert as_of.creators_json is None
        assert latest_observation_as_of(session, token.id, NOW - timedelta(minutes=1)) is None
        latest = latest_observation_as_of(session, token.id, NOW + timedelta(hours=3))
        assert latest is not None
        assert latest.creator_vector_status == STATUS_VERIFIED
        rows = list(session.scalars(select(MetaplexCreatorObservation).order_by(MetaplexCreatorObservation.id)).all())
        assert len(rows) == 3
        assert as_utc(rows[0].observed_at) == NOW
        assert session.get(CreatorObservation, init_id).parser_notes == "preexisting init"
        assert session.get(TokenAccountOwnerObservation, holder_id) is not None
    finally:
        session.close()


def test_scan_and_api(client) -> None:
    session = get_session_factory()()
    try:
        _clean(session)
        session.execute(delete(MetaplexCreatorObservation))
        session.commit()
        session.add(Token(token_address=MINT, symbol="D", name="Doge", chain="solana"))
        session.commit()
        script = _script_for(_creators_encoded([(C1, 1, 100)]))

        async def body():
            async with script.client() as rpc:
                return await scan_metaplex_creators(session, rpc, get_settings(), limit=5)

        summary = run(body())
        assert summary.tokens_scanned == 1
        assert summary.verified == 1
        assert summary.applied_limit == 5
        assert summary.rpc_calls == 1
        scan = session.scalar(select(IntelligenceScan).where(IntelligenceScan.scan_type == "metaplex_creators"))
        assert scan is not None
    finally:
        session.close()

    status = client.get("/api/v1/creator/metadata/status")
    assert status.status_code == 200
    payload = client.get(f"/api/v1/creator/metadata/{MINT}").json()
    observation = payload["observation"]
    assert observation["creator_vector_status"] == STATUS_VERIFIED
    assert observation["creators"][0]["share"] == 100
    assert "creator_vector_status" not in observation["unknown_fields"]
    history = client.get(f"/api/v1/creator/metadata/{MINT}/history")
    assert history.status_code == 200
    assert len(history.json()["observations"]) == 1
    missing = client.get("/api/v1/creator/metadata/UnknownMint1111111111111111111111111111111")
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
    script = _script_for(None)

    async def body():
        async with script.client() as rpc:
            return await inspect_metaplex_creators(MINT, rpc)

    result = run(body())
    assert result.creators is None
    assert result.creator_count is None
    assert result.verified_creator_count is None
    assert result.observed_at is None
    assert result.slot is None
    assert result.creator_vector_status == STATUS_UNAVAILABLE
