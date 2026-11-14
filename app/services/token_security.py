"""Mint/freeze/program checks from Solana parsed mint accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class TokenSecurityResult:
    mint_authority: str | None = None
    mint_authority_status: str = "UNKNOWN"
    freeze_authority: str | None = None
    freeze_authority_status: str = "UNKNOWN"
    token_program: str | None = None
    token_standard: str | None = None
    supply: float | None = None
    decimals: int | None = None
    metadata_mutability: str = "UNKNOWN"
    metadata_status: str = "UNAVAILABLE"
    metadata_account: str | None = None
    metadata_update_authority: str | None = None
    metadata_name: str | None = None
    metadata_symbol: str | None = None
    metadata_uri: str | None = None
    metadata_is_mutable: bool | None = None
    slot: int | None = None
    observed_at: datetime | None = None
    source: str = "solana_rpc"
    provider: str = "solana_rpc"
    token_2022_inspection: Any | None = None
    findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    available: bool = False


async def analyze_token_security(mint: str, rpc: SolanaRpcClient) -> TokenSecurityResult:
    from app.services.metaplex_metadata import fetch_metaplex_metadata
    from app.services.token_2022_extensions import inspect_token_2022_mint

    result = TokenSecurityResult()
    try:
        mint_account = await rpc.get_account_info(mint, encoding="jsonParsed")
    except SolanaRpcError as ext_exc:
        result.errors.append(str(ext_exc))
        result.findings.append("Mint account lookup failed. Missing data is not treated as safe.")
        result.metadata_status = "UNAVAILABLE"
        result.token_2022_inspection = inspect_token_2022_mint(mint, None, rpc_error=str(ext_exc))
        result.errors.extend(result.token_2022_inspection.errors)
        result.findings.extend(result.token_2022_inspection.findings)
        return result

    result.slot = mint_account.slot
    if mint_account.observed_at is not None:
        result.observed_at = mint_account.observed_at
    result.token_2022_inspection = inspect_token_2022_mint(mint, mint_account)
    account = mint_account.value
    if account is None:
        result.errors.append("Mint account not found")
        result.findings.append("No parsed mint account. Authority status remains UNKNOWN.")
        result.metadata_status = "NOT_FOUND"
        if result.token_2022_inspection is not None:
            result.errors.extend(result.token_2022_inspection.errors)
            result.findings.extend(result.token_2022_inspection.findings)
        return result

    result.available = True
    owner = account.get("owner")
    result.token_program = owner if isinstance(owner, str) else None
    if result.token_program == TOKEN_2022_PROGRAM:
        result.token_standard = "token-2022"
    elif result.token_program == TOKEN_PROGRAM:
        result.token_standard = "spl-token"
    else:
        result.token_standard = "unknown"

    parsed = _parsed_info(account)
    result.mint_authority, result.mint_authority_status = _authority(parsed.get("mintAuthority"))
    result.freeze_authority, result.freeze_authority_status = _authority(parsed.get("freezeAuthority"))
    result.decimals = _as_int(parsed.get("decimals"))
    result.supply = _as_float(parsed.get("supply"))
    if result.supply is None:
        try:
            supply_info = await rpc.get_token_supply(mint)
            if isinstance(supply_info, dict):
                result.supply = _as_float(supply_info.get("uiAmount")) or _as_float(supply_info.get("amount"))
                if result.decimals is None:
                    result.decimals = _as_int(supply_info.get("decimals"))
        except SolanaRpcError as exc:
            result.errors.append(str(exc))

    metadata = await fetch_metaplex_metadata(
        mint,
        rpc,
        token_program=result.token_program,
        observed_at=result.observed_at,
    )
    result.metadata_status = metadata.status
    result.metadata_account = metadata.metadata_account
    result.metadata_update_authority = metadata.update_authority
    result.metadata_name = metadata.name
    result.metadata_symbol = metadata.symbol
    result.metadata_uri = metadata.uri
    result.metadata_is_mutable = metadata.is_mutable
    result.metadata_mutability = metadata.mutability_label
    if metadata.slot is not None:
        result.slot = metadata.slot
    result.findings.extend(metadata.findings)
    result.errors.extend(metadata.errors)
    if result.token_2022_inspection is not None:
        result.findings.extend(result.token_2022_inspection.findings)
        result.errors.extend(result.token_2022_inspection.errors)

    if result.mint_authority_status == "VERIFIED_PRESENT":
        result.findings.append("Mint authority is active; additional supply can be minted.")
    elif result.mint_authority_status == "VERIFIED_REVOKED":
        result.findings.append("Mint authority is null on-chain (cannot mint from this field).")
    if result.freeze_authority_status == "VERIFIED_PRESENT":
        result.findings.append("Freeze authority is active; token accounts can be frozen.")
    elif result.freeze_authority_status == "VERIFIED_REVOKED":
        result.findings.append("Freeze authority is null on-chain.")
    return result


def _parsed_info(account: dict[str, Any]) -> dict[str, Any]:
    data = account.get("data")
    if not isinstance(data, dict):
        return {}
    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        return {}
    info = parsed.get("info")
    return info if isinstance(info, dict) else {}


def _authority(value: Any) -> tuple[str | None, str]:
    if value is None:
        return None, "VERIFIED_REVOKED"
    if isinstance(value, str) and value.strip():
        return value.strip(), "VERIFIED_PRESENT"
    return None, "UNKNOWN"


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
