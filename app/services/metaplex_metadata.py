"""Read-only Metaplex Token Metadata PDA lookup via Solana RPC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.solana_pda import PdaError, b58decode, b58encode, find_program_address, is_valid_pubkey
from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

METADATA_PROGRAM_ID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
METADATA_KEY_V1 = 4
MAX_CREATORS = 5
AVAILABLE = "AVAILABLE"
NOT_FOUND = "NOT_FOUND"
UNSUPPORTED = "UNSUPPORTED"
UNAVAILABLE = "UNAVAILABLE"
MALFORMED = "MALFORMED"


@dataclass
class MetaplexMetadataResult:
    status: str = UNAVAILABLE
    metadata_account: str | None = None
    update_authority: str | None = None
    is_mutable: bool | None = None
    name: str | None = None
    symbol: str | None = None
    uri: str | None = None
    slot: int | None = None
    observed_at: datetime | None = None
    source: str = "solana_rpc"
    findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def mutability_label(self) -> str:
        if self.status != AVAILABLE or self.is_mutable is None:
            return "UNKNOWN"
        return "MUTABLE" if self.is_mutable else "IMMUTABLE"


def derive_metadata_pda(mint: str) -> str:
    mint_bytes = b58decode(mint)
    program = b58decode(METADATA_PROGRAM_ID)
    address, _bump = find_program_address([b"metadata", program, mint_bytes], program)
    return b58encode(address)


async def fetch_metaplex_metadata(
    mint: str,
    rpc: SolanaRpcClient,
    *,
    token_program: str | None = None,
    observed_at: datetime | None = None,
) -> MetaplexMetadataResult:
    result = MetaplexMetadataResult(observed_at=observed_at)
    try:
        result.metadata_account = derive_metadata_pda(mint)
    except PdaError as exc:
        result.status = UNAVAILABLE
        result.errors.append(str(exc))
        result.findings.append("Metadata PDA could not be derived. Missing metadata is not treated as malicious.")
        return result

    try:
        account = await rpc.get_account_info(result.metadata_account, encoding="base64")
    except SolanaRpcError as exc:
        result.status = UNAVAILABLE
        result.errors.append(str(exc))
        result.findings.append(
            "Metaplex metadata RPC lookup failed. Unavailable metadata is not evidence of safety or of a scam."
        )
        return result

    result.slot = account.slot
    if account.observed_at is not None:
        result.observed_at = account.observed_at
    if account.value is None:
        result.status = UNSUPPORTED if token_program == TOKEN_2022_PROGRAM else NOT_FOUND
        if result.status == NOT_FOUND:
            result.findings.append(
                "No Metaplex Token Metadata account at the derived PDA. Missing metadata is not malicious."
            )
        else:
            result.findings.append(
                "No Metaplex Token Metadata PDA. Token-2022 metadata extensions were not parsed in this milestone."
            )
        return result

    owner = account.value.get("owner")
    if not isinstance(owner, str) or owner != METADATA_PROGRAM_ID:
        result.status = MALFORMED
        result.errors.append(
            "Metadata account owner is missing"
            if owner is None
            else f"Metadata account owner is not the Metaplex Token Metadata program: {owner}"
        )
        result.findings.append(
            "Account at the metadata PDA is not owned by the Metaplex Token Metadata program. "
            "Bytes were not decoded. This is not treated as evidence of a scam."
        )
        return result

    try:
        raw = _account_bytes(account.value)
        decoded = decode_metadata_account(raw)
    except MetadataDecodeError as exc:
        result.status = MALFORMED
        result.errors.append(str(exc))
        result.findings.append("Metadata account bytes could not be decoded. No fields were invented.")
        return result

    result.status = AVAILABLE
    result.update_authority = decoded["update_authority"]
    result.is_mutable = decoded["is_mutable"]
    result.name = decoded["name"]
    result.symbol = decoded["symbol"]
    result.uri = decoded["uri"]
    result.findings.append(
        "Metaplex metadata was observed from current RPC state. "
        "Mutable metadata is not automatically a scam. Immutable metadata is not proof of safety. "
        "This read does not reconstruct historical chain state."
    )
    return result


class MetadataDecodeError(ValueError):
    pass


def decode_metadata_account(data: bytes) -> dict[str, Any]:
    if len(data) < 1 + 32 + 32 + 4:
        raise MetadataDecodeError("Metadata account too short")
    offset = 0
    key = data[offset]
    offset += 1
    if key != METADATA_KEY_V1:
        raise MetadataDecodeError(f"Unsupported metadata key {key}")
    update_authority = b58encode(data[offset : offset + 32])
    offset += 32
    mint, offset = _read_pubkey(data, offset)
    name, offset = _borsh_string(data, offset)
    symbol, offset = _borsh_string(data, offset)
    uri, offset = _borsh_string(data, offset)
    if offset + 3 > len(data):
        raise MetadataDecodeError("Metadata truncated before mutability flag")
    offset += 2  # seller_fee_basis_points
    creators_option = data[offset]
    offset += 1
    creators, offset = _skip_or_read_creators(data, offset, creators_option, parse_entries=False)
    if offset + 2 > len(data):
        raise MetadataDecodeError("Metadata truncated before is_mutable")
    offset += 1  # primary_sale_happened
    is_mutable = data[offset] != 0
    return {
        "update_authority": update_authority,
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "is_mutable": is_mutable,
        "creators_option": creators_option,
        "creators": creators,
    }


def decode_metadata_creator_vector(data: bytes) -> dict[str, Any]:
    """Decode metadata including a strictly validated creators option/vector."""
    if len(data) < 1 + 32 + 32 + 4:
        raise MetadataDecodeError("Metadata account too short")
    offset = 0
    key = data[offset]
    offset += 1
    if key != METADATA_KEY_V1:
        raise MetadataDecodeError(f"Unsupported metadata key {key}")
    update_authority, offset = _read_pubkey(data, offset)
    mint, offset = _read_pubkey(data, offset)
    name, offset = _borsh_string(data, offset)
    symbol, offset = _borsh_string(data, offset)
    uri, offset = _borsh_string(data, offset)
    if offset + 3 > len(data):
        raise MetadataDecodeError("Metadata truncated before creator option")
    offset += 2
    creators_option = data[offset]
    offset += 1
    creators, offset = _skip_or_read_creators(data, offset, creators_option, parse_entries=True)
    if offset + 2 > len(data):
        raise MetadataDecodeError("Metadata truncated before is_mutable")
    offset += 1
    is_mutable = data[offset] != 0
    return {
        "update_authority": update_authority,
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "is_mutable": is_mutable,
        "creators_option": creators_option,
        "creators": creators,
    }


def encode_metadata_account(
    *,
    update_authority: bytes,
    mint: bytes,
    name: str,
    symbol: str,
    uri: str,
    is_mutable: bool,
    creators: list[tuple[bytes, int, int]] | None = None,
    creators_option: int | None = None,
) -> bytes:
    payload = bytearray()
    payload.append(METADATA_KEY_V1)
    payload.extend(update_authority)
    payload.extend(mint)
    payload.extend(_borsh_encode_string(name))
    payload.extend(_borsh_encode_string(symbol))
    payload.extend(_borsh_encode_string(uri))
    payload.extend((0).to_bytes(2, "little"))
    if creators_option is None:
        creators_option = 0 if creators is None else 1
    payload.append(creators_option)
    if creators_option == 1:
        entries = creators if creators is not None else []
        payload.extend(len(entries).to_bytes(4, "little"))
        for address, verified, share in entries:
            payload.extend(address)
            payload.append(int(verified))
            payload.append(int(share))
    payload.append(0)  # primary sale
    payload.append(1 if is_mutable else 0)
    return bytes(payload)


def _account_bytes(value: dict[str, Any]) -> bytes:
    data = value.get("data")
    if isinstance(data, list) and data:
        raw = data[0]
        if isinstance(raw, str):
            import base64

            try:
                return base64.b64decode(raw)
            except Exception as exc:  # noqa: BLE001
                raise MetadataDecodeError("Invalid base64 metadata data") from exc
    if isinstance(data, str):
        import base64

        try:
            return base64.b64decode(data)
        except Exception as exc:  # noqa: BLE001
            raise MetadataDecodeError("Invalid base64 metadata data") from exc
    raise MetadataDecodeError("Metadata account data is not base64 bytes")


def _borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        raise MetadataDecodeError("Metadata string length missing")
    length = int.from_bytes(data[offset : offset + 4], "little")
    offset += 4
    if length < 0 or offset + length > len(data):
        raise MetadataDecodeError("Metadata string overruns account data")
    raw = data[offset : offset + length]
    offset += length
    try:
        text = raw.split(b"\x00", 1)[0].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise MetadataDecodeError("Metadata string is not valid UTF-8") from exc
    return text or None, offset  # type: ignore[return-value]


def require_creator_address(address: str, index: int | None = None) -> str:
    label = f"Creator {index} address" if index is not None else "Creator address"
    if not is_valid_pubkey(address):
        raise MetadataDecodeError(f"{label} is not a valid public key")
    return address


def _read_pubkey(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 32 > len(data):
        raise MetadataDecodeError("Metadata truncated in public key")
    return b58encode(data[offset : offset + 32]), offset + 32


def _skip_or_read_creators(
    data: bytes,
    offset: int,
    creators_option: int,
    *,
    parse_entries: bool,
) -> tuple[list[dict[str, Any]] | None, int]:
    if creators_option == 0:
        return None, offset
    if creators_option != 1:
        raise MetadataDecodeError(f"Invalid creators option {creators_option}")
    if offset + 4 > len(data):
        raise MetadataDecodeError("Metadata truncated in creators vec")
    count = int.from_bytes(data[offset : offset + 4], "little")
    offset += 4
    if count < 0:
        raise MetadataDecodeError("Creators vec count is invalid")
    if parse_entries:
        if count > MAX_CREATORS:
            raise MetadataDecodeError(f"Creators vec count {count} exceeds Metaplex maximum {MAX_CREATORS}")
        creators: list[dict[str, Any]] = []
        for index in range(count):
            entry, offset = _read_creator_entry(data, offset, index)
            creators.append(entry)
        if creators:
            shares = [int(item["share"]) for item in creators if item.get("share") is not None]
            if len(shares) != len(creators) or sum(shares) != 100:
                raise MetadataDecodeError("Creator shares must be present and sum to 100")
        return creators, offset
    creators_bytes = count * 34
    if offset + creators_bytes > len(data):
        raise MetadataDecodeError("Metadata truncated in creators vec")
    return [], offset + creators_bytes


def _read_creator_entry(data: bytes, offset: int, index: int) -> tuple[dict[str, Any], int]:
    if offset + 34 > len(data):
        raise MetadataDecodeError("Metadata truncated in creators vec")
    address_bytes = data[offset : offset + 32]
    offset += 32
    if len(address_bytes) != 32:
        raise MetadataDecodeError(f"Creator {index} address is not 32 bytes")
    address = require_creator_address(b58encode(address_bytes), index)
    verified_raw = data[offset]
    offset += 1
    if verified_raw not in (0, 1):
        raise MetadataDecodeError(f"Creator {index} verified flag is invalid")
    share = data[offset]
    offset += 1
    if share > 100:
        raise MetadataDecodeError(f"Creator {index} share {share} is not a valid percentage")
    return (
        {
            "address": address,
            "verified": bool(verified_raw),
            "share": int(share),
            "order": index,
        },
        offset,
    )


def _borsh_encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "little") + encoded
