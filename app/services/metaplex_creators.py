"""Metaplex Token Metadata creators[] evidence. Not identity, not a deployer claim."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MetaplexCreatorObservation
from app.services.metaplex_metadata import (
    MALFORMED,
    METADATA_PROGRAM_ID,
    NOT_FOUND,
    UNAVAILABLE,
    MetadataDecodeError,
    decode_metadata_creator_vector,
    derive_metadata_pda,
    _account_bytes,
)
from app.services.rpc_accounting import KIND_RATE_LIMITED, KIND_TIMEOUT, RpcAccounting, attach_accounting
from app.services.solana_pda import PdaError, is_valid_pubkey
from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError
from app.timeutil import as_utc, utc_now

STATUS_VERIFIED = "VERIFIED"
STATUS_VERIFIED_ABSENT = "VERIFIED_ABSENT"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_UNAVAILABLE = "UNAVAILABLE"
SOURCE = "solana_rpc"


@dataclass
class MetaplexCreatorInspection:
    mint: str
    metadata_account: str | None = None
    metadata_status: str = STATUS_UNAVAILABLE
    creator_vector_status: str = STATUS_UNKNOWN
    creators: list[dict[str, Any]] | None = None
    creator_count: int | None = None
    verified_creator_count: int | None = None
    source: str = SOURCE
    observed_at: datetime | None = None
    attempted_at: datetime | None = None
    slot: int | None = None
    parser_notes: str | None = None
    errors: list[str] = field(default_factory=list)
    rpc_calls: int = 0
    rpc_successes: int = 0
    rpc_failures: int = 0
    rpc_retries: int = 0
    rpc_http_429: int = 0
    rpc_timeouts: int = 0
    rpc_other_errors: int = 0
    rpc_cache_hits: int = 0
    rpc_accounting: RpcAccounting | None = None
    from_cache: bool = False


async def inspect_metaplex_creators(mint: str, rpc: SolanaRpcClient) -> MetaplexCreatorInspection:
    result = MetaplexCreatorInspection(mint=mint.strip(), attempted_at=utc_now())
    start = rpc.accounting.copy()
    try:
        return await _inspect_metaplex_creators(result, rpc)
    finally:
        attach_accounting(result, rpc.accounting.delta(start), rpc.last_attempt_at or result.attempted_at)


async def _inspect_metaplex_creators(
    result: MetaplexCreatorInspection,
    rpc: SolanaRpcClient,
) -> MetaplexCreatorInspection:
    if not is_valid_pubkey(result.mint):
        result.metadata_status = STATUS_UNKNOWN
        result.creator_vector_status = STATUS_UNKNOWN
        result.parser_notes = "Mint address is not a valid public key."
        result.errors.append("Invalid mint address")
        return result
    try:
        result.metadata_account = derive_metadata_pda(result.mint)
    except PdaError as exc:
        result.metadata_status = STATUS_UNAVAILABLE
        result.creator_vector_status = STATUS_UNAVAILABLE
        result.errors.append(str(exc))
        result.parser_notes = "Metadata PDA could not be derived. Creator vector is UNAVAILABLE."
        return result

    try:
        account = await rpc.get_account_info(result.metadata_account, encoding="base64")
        result.from_cache = account.from_cache
    except SolanaRpcError as exc:
        _apply_rpc_error(result, exc)
        result.parser_notes = (
            "Metadata account RPC lookup failed. Creator evidence is UNAVAILABLE, not VERIFIED_ABSENT."
        )
        return result

    if account.value is None:
        result.metadata_status = NOT_FOUND
        result.creator_vector_status = STATUS_UNAVAILABLE
        result.parser_notes = (
            "No metadata account at the derived PDA. Missing account data is UNAVAILABLE, "
            "not a verified empty creator vector."
        )
        return result

    result.slot = account.slot
    owner = account.value.get("owner")
    if not isinstance(owner, str) or owner != METADATA_PROGRAM_ID:
        result.metadata_status = MALFORMED
        result.creator_vector_status = STATUS_UNKNOWN
        result.errors.append(
            "Metadata account owner is missing"
            if owner is None
            else f"Metadata account owner is not the Metaplex Token Metadata program: {owner}"
        )
        result.parser_notes = (
            "Account at the metadata PDA is not owned by the Metaplex program. "
            "Creator bytes were not decoded."
        )
        return result

    try:
        raw = _account_bytes(account.value)
        decoded = decode_metadata_creator_vector(raw)
    except MetadataDecodeError as exc:
        result.metadata_status = MALFORMED
        result.creator_vector_status = STATUS_UNKNOWN
        result.observed_at = account.observed_at
        result.errors.append(str(exc))
        result.parser_notes = "Metadata creator vector could not be decoded. No creator fields were invented."
        return result

    if decoded["mint"] != result.mint:
        result.metadata_status = MALFORMED
        result.creator_vector_status = STATUS_UNKNOWN
        result.observed_at = account.observed_at
        result.errors.append(
            f"Decoded metadata mint {decoded['mint']} does not match requested mint {result.mint}"
        )
        result.parser_notes = "Metadata mint mismatch. Creator vector is not attached as verified evidence."
        return result

    result.observed_at = account.observed_at
    result.metadata_status = "AVAILABLE"
    option = decoded["creators_option"]
    creators = decoded["creators"]
    if option == 0:
        result.creators = None
        result.creator_count = 0
        result.verified_creator_count = 0
        result.creator_vector_status = STATUS_VERIFIED_ABSENT
        result.parser_notes = (
            "Creators option is explicitly absent (Option::None). "
            "This is not a person, mint-init signer, fee payer, or mint authority."
        )
        return result
    if creators == []:
        result.creators = []
        result.creator_count = 0
        result.verified_creator_count = 0
        result.creator_vector_status = STATUS_VERIFIED_ABSENT
        result.parser_notes = (
            "Creators option is present with a valid empty vector. "
            "A Metaplex verified flag is not a real-world identity."
        )
        return result

    result.creators = creators
    result.creator_count = len(creators)
    result.verified_creator_count = sum(1 for item in creators if item.get("verified") is True)
    result.creator_vector_status = STATUS_VERIFIED
    result.parser_notes = (
        "Metaplex creators[] was decoded from the current metadata account. "
        "verified=true is the on-chain metadata flag only. It does not prove identity, "
        "deployer status, ownership, or control. Creators are not merged with mint-init signers, "
        "fee payers, mint authority, update authority, or token-account owners."
    )
    return result


def latest_observation_as_of(
    session: Session,
    token_id: int,
    as_of: datetime,
) -> MetaplexCreatorObservation | None:
    cutoff = as_utc(as_of)
    rows = list(
        session.scalars(
            select(MetaplexCreatorObservation)
            .where(MetaplexCreatorObservation.token_id == token_id)
            .order_by(MetaplexCreatorObservation.id.desc())
        ).all()
    )
    eligible: list[MetaplexCreatorObservation] = []
    for row in rows:
        observed = as_utc(row.observed_at) if row.observed_at is not None else None
        if observed is None or cutoff is None or observed > cutoff:
            continue
        eligible.append(row)
    if not eligible:
        return None
    eligible.sort(key=lambda item: (as_utc(item.observed_at) or cutoff, item.id), reverse=True)
    return eligible[0]


def latest_metaplex_creator_observation(
    session: Session, token_id: int
) -> MetaplexCreatorObservation | None:
    return session.scalar(
        select(MetaplexCreatorObservation)
        .where(MetaplexCreatorObservation.token_id == token_id)
        .order_by(MetaplexCreatorObservation.id.desc())
        .limit(1)
    )


def metaplex_creator_history(session: Session, token_id: int) -> list[MetaplexCreatorObservation]:
    return list(
        session.scalars(
            select(MetaplexCreatorObservation)
            .where(MetaplexCreatorObservation.token_id == token_id)
            .order_by(MetaplexCreatorObservation.id.asc())
        ).all()
    )


def _apply_rpc_error(result: MetaplexCreatorInspection, exc: SolanaRpcError) -> None:
    message = str(exc)
    result.errors.append(message)
    unavailable = (
        exc.is_rate_limited
        or exc.kind in {KIND_RATE_LIMITED, KIND_TIMEOUT}
        or exc.status_code in {408, 429, 500, 502, 503, 504}
        or "Timeout" in message
        or "Rate limited" in message
        or "Retryable HTTP" in message
    )
    result.metadata_status = UNAVAILABLE
    result.creator_vector_status = STATUS_UNAVAILABLE if unavailable else STATUS_UNKNOWN
    if not unavailable:
        result.metadata_status = STATUS_UNKNOWN
