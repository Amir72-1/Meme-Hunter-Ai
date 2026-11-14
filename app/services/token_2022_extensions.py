"""Parse Token-2022 mint extensions from Solana jsonParsed RPC data.

Inspection records evidence only. Presence of an extension is not a scam,
honeypot, or unsellable classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Token2022ExtensionObservation
from app.services.solana_pda import is_valid_pubkey
from app.services.solana_rpc import RpcAccountResult
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from app.timeutil import as_utc

STATUS_AVAILABLE = "AVAILABLE"
STATUS_NO_EXTENSIONS = "NO_EXTENSIONS"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_UNSUPPORTED = "UNSUPPORTED"

# jsonParsed serde names (camelCase). Additional types are preserved as returned.
KNOWN_EXTENSION_TYPES = frozenset(
    {
        "transferFeeConfig",
        "transferHook",
        "permanentDelegate",
        "defaultAccountState",
        "nonTransferable",
        "pausable",
        "mintCloseAuthority",
        "confidentialTransferMint",
        "metadataPointer",
        "tokenMetadata",
        "scaledUiAmount",
        "interestBearingConfig",
        "transferFeeAmount",
        "confidentialTransferAccount",
        "confidentialTransferFeeConfig",
        "confidentialTransferFeeAmount",
        "transferHookAccount",
        "metadataPointer",
        "groupPointer",
        "groupMemberPointer",
        "tokenGroup",
        "tokenGroupMember",
        "confidentialMintBurn",
        "pausableAccount",
        "scaledUiAmountConfig",
        "unparseableExtension",
    }
)


@dataclass
class Token2022Inspection:
    mint: str
    token_program: str | None = None
    inspection_status: str = STATUS_UNAVAILABLE
    extension_types: list[str] = field(default_factory=list)
    parsed_fields: dict[str, Any] = field(default_factory=dict)
    transfer_fee_bps: int | None = None
    transfer_fee_max: str | None = None
    transfer_fee_config_authority: str | None = None
    transfer_fee_withdraw_authority: str | None = None
    transfer_hook_program_id: str | None = None
    transfer_hook_authority: str | None = None
    permanent_delegate: str | None = None
    default_account_state: str | None = None
    pausable_paused: bool | None = None
    pausable_authority: str | None = None
    mint_close_authority: str | None = None
    confidential_transfer_present: bool | None = None
    confidential_auto_approve: bool | None = None
    metadata_pointer_address: str | None = None
    metadata_pointer_authority: str | None = None
    token_metadata_present: bool | None = None
    scaled_ui_amount_multiplier: str | None = None
    parser_complete: bool = False
    parser_notes: str | None = None
    source: str = "solana_rpc"
    observed_at: datetime | None = None
    slot: int | None = None
    from_cache: bool = False
    errors: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


def inspect_token_2022_mint(
    mint: str,
    mint_account: RpcAccountResult | None,
    *,
    rpc_error: str | None = None,
) -> Token2022Inspection:
    """Inspect Token-2022 extensions from a jsonParsed mint account.

    Does not invent extension values. An empty list is NO_EXTENSIONS only when
    the parser and account format provide a complete inspection.
    """
    result = Token2022Inspection(mint=mint)
    if rpc_error:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append(rpc_error)
        result.parser_notes = "RPC failure; no successful observation timestamp."
        result.findings.append("Token-2022 extension inspection failed. Missing data is not treated as safe.")
        return result

    if mint_account is None:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_notes = "No mint account result."
        result.errors.append("Mint account result missing")
        return result

    result.slot = mint_account.slot
    result.observed_at = mint_account.observed_at
    result.from_cache = mint_account.from_cache
    account = mint_account.value
    if account is None:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append("Mint account not found")
        result.parser_notes = "Account missing; cannot conclude extension absence."
        return result
    if not isinstance(account, dict):
        result.inspection_status = STATUS_UNAVAILABLE
        result.errors.append("Mint account is not an object")
        result.parser_notes = "Malformed RPC account value."
        return result

    owner = account.get("owner")
    result.token_program = owner if isinstance(owner, str) else None
    if result.token_program == TOKEN_PROGRAM:
        result.inspection_status = STATUS_UNSUPPORTED
        result.parser_complete = True
        result.parser_notes = "Classic SPL Token mint is outside Token-2022 extension inspection."
        result.findings.append("Token-2022 extension inspection does not apply to the classic SPL Token program.")
        return result
    if result.token_program != TOKEN_2022_PROGRAM:
        result.inspection_status = STATUS_UNSUPPORTED
        result.parser_complete = False
        result.parser_notes = "Account owner is not the Token-2022 program."
        result.errors.append(f"Unsupported token program owner: {result.token_program}")
        return result

    data = account.get("data")
    if isinstance(data, list):
        result.inspection_status = STATUS_UNSUPPORTED
        result.parser_complete = False
        result.parser_notes = "Unsupported binary/base64 mint representation; jsonParsed extensions were not provided."
        result.errors.append("Mint data is not jsonParsed")
        result.findings.append("Token-2022 extensions were not decoded. Unsupported representation is not treated as safe.")
        return result
    if not isinstance(data, dict):
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append("Mint data is malformed")
        result.parser_notes = "Malformed RPC mint data object."
        return result

    program = data.get("program")
    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append("jsonParsed mint payload missing")
        result.parser_notes = "Incomplete jsonParsed response; empty extensions cannot be inferred."
        return result
    parsed_type = parsed.get("type")
    info = parsed.get("info")
    if parsed_type != "mint" or not isinstance(info, dict):
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append("jsonParsed payload is not a mint info object")
        result.parser_notes = "Incomplete mint parse; cannot conclude extension absence."
        return result
    if program not in ("spl-token-2022", "spl-token"):
        result.errors.append(f"Unexpected jsonParsed program tag: {program}")
        result.parser_notes = "jsonParsed program tag is not a known Token-2022 mint tag."
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        return result

    raw_extensions = info.get("extensions")
    if raw_extensions is None and isinstance(parsed.get("extensions"), list):
        raw_extensions = parsed.get("extensions")

    if raw_extensions is None:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.parser_notes = (
            "jsonParsed mint omitted the extensions field. "
            "An omitted list is not evidence that no extensions exist."
        )
        result.errors.append("extensions field missing from jsonParsed mint info")
        result.findings.append("Token-2022 extension list was not provided by the RPC parser.")
        return result

    if not isinstance(raw_extensions, list):
        result.inspection_status = STATUS_UNSUPPORTED
        result.parser_complete = False
        result.parser_notes = "Unsupported extensions representation (not a jsonParsed list)."
        result.errors.append("extensions field is not a list")
        return result

    types: list[str] = []
    parsed_fields: dict[str, Any] = {}
    incomplete_items = False
    unparseable = False
    for item in raw_extensions:
        if not isinstance(item, dict):
            incomplete_items = True
            result.errors.append("Extension list contains a non-object item")
            continue
        name = item.get("extension")
        if not isinstance(name, str) or not name.strip():
            incomplete_items = True
            result.errors.append("Extension item missing type name")
            continue
        name = name.strip()
        types.append(name)
        if name == "unparseableExtension":
            unparseable = True
            parsed_fields[name] = {"unparseable": True}
            continue
        state = item.get("state")
        if state is None:
            parsed_fields[name] = {"present": True}
            _apply_known_extension(result, name, {})
            continue
        if not isinstance(state, dict):
            incomplete_items = True
            result.errors.append(f"Extension {name} state is not an object")
            parsed_fields[name] = {"present": True, "state_unparsed": True}
            continue
        parsed_fields[name] = _safe_state(state)
        _apply_known_extension(result, name, state)

    result.extension_types = types
    result.parsed_fields = parsed_fields
    identity_error = _mint_identity_error(mint, parsed_fields)
    if identity_error:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.errors.append(identity_error)
        result.parser_notes = "Mint identity mismatch; observation is not attached as a successful inspection."
        result.findings.append("Parsed TokenMetadata mint does not match the requested mint.")
        return result

    if incomplete_items or unparseable:
        result.inspection_status = STATUS_UNAVAILABLE
        result.parser_complete = False
        result.parser_notes = (
            "Extension list was incomplete or contained an unparseable item. "
            "Partial data is not treated as a complete inspection."
        )
        result.findings.append("Token-2022 extension inspection is incomplete. Missing data is not treated as safe.")
        _evidence_findings(result)
        return result

    result.parser_complete = True
    if not types:
        result.inspection_status = STATUS_NO_EXTENSIONS
        result.parser_notes = "jsonParsed Token-2022 mint provided a complete empty extensions list."
        result.findings.append("Token-2022 mint has a verified empty jsonParsed extension list.")
        return result

    result.inspection_status = STATUS_AVAILABLE
    result.parser_notes = "jsonParsed Token-2022 mint extensions were decoded without inventing fields."
    _evidence_findings(result)
    return result


def latest_observation_as_of(
    session: Session,
    token_id: int,
    as_of: datetime,
) -> Token2022ExtensionObservation | None:
    """Return the latest Token-2022 observation with observed_at <= as_of."""
    cutoff = as_utc(as_of)
    rows = list(
        session.scalars(
            select(Token2022ExtensionObservation)
            .where(Token2022ExtensionObservation.token_id == token_id)
            .order_by(Token2022ExtensionObservation.observed_at.desc())
        ).all()
    )
    eligible: list[Token2022ExtensionObservation] = []
    for row in rows:
        observed = as_utc(row.observed_at) if row.observed_at is not None else None
        if observed is None or cutoff is None or observed > cutoff:
            continue
        eligible.append(row)
    if not eligible:
        return None
    eligible.sort(key=lambda item: as_utc(item.observed_at) or cutoff, reverse=True)
    return eligible[0]


def _apply_known_extension(result: Token2022Inspection, name: str, state: dict[str, Any]) -> None:
    if name == "transferFeeConfig":
        newer = state.get("newerTransferFee") if isinstance(state.get("newerTransferFee"), dict) else {}
        result.transfer_fee_bps = _as_int(newer.get("transferFeeBasisPoints"))
        max_fee = newer.get("maximumFee")
        result.transfer_fee_max = str(max_fee) if max_fee is not None else None
        result.transfer_fee_config_authority = _pubkey(state.get("transferFeeConfigAuthority"))
        result.transfer_fee_withdraw_authority = _pubkey(state.get("withdrawWithheldAuthority"))
    elif name == "transferHook":
        result.transfer_hook_program_id = _pubkey(state.get("programId"))
        result.transfer_hook_authority = _pubkey(state.get("authority"))
    elif name == "permanentDelegate":
        result.permanent_delegate = _pubkey(state.get("delegate"))
    elif name == "defaultAccountState":
        account_state = state.get("accountState") or state.get("state")
        result.default_account_state = str(account_state) if account_state is not None else None
    elif name == "nonTransferable":
        result.parsed_fields.setdefault(name, {"present": True})
    elif name in ("pausable", "pausableConfig"):
        if "paused" in state:
            result.pausable_paused = _as_bool(state.get("paused"))
        result.pausable_authority = _pubkey(state.get("authority"))
    elif name == "mintCloseAuthority":
        result.mint_close_authority = _pubkey(state.get("closeAuthority") or state.get("authority"))
    elif name == "confidentialTransferMint":
        result.confidential_transfer_present = True
        if "autoApproveNewAccounts" in state:
            result.confidential_auto_approve = _as_bool(state.get("autoApproveNewAccounts"))
    elif name == "metadataPointer":
        result.metadata_pointer_address = _pubkey(state.get("metadataAddress"))
        result.metadata_pointer_authority = _pubkey(state.get("authority"))
    elif name == "tokenMetadata":
        result.token_metadata_present = True
    elif name in ("scaledUiAmount", "scaledUiAmountConfig"):
        multiplier = state.get("multiplier") or state.get("newMultiplier")
        if multiplier is not None:
            result.scaled_ui_amount_multiplier = str(multiplier)


def _mint_identity_error(requested_mint: str, parsed_fields: dict[str, Any]) -> str | None:
    meta = parsed_fields.get("tokenMetadata")
    if not isinstance(meta, dict):
        return None
    meta_mint = meta.get("mint")
    if meta_mint is None or meta_mint == "":
        return None
    if not isinstance(meta_mint, str):
        return "TokenMetadata mint is not a string"
    if is_valid_pubkey(meta_mint) and meta_mint.strip() != requested_mint.strip():
        return f"TokenMetadata mint {meta_mint} does not match requested mint {requested_mint}"
    return None


def _evidence_findings(result: Token2022Inspection) -> None:
    if result.transfer_fee_bps is not None:
        result.findings.append(
            f"TransferFeeConfig present (basis points={result.transfer_fee_bps}). Evidence only; not a scam label."
        )
    if result.transfer_hook_program_id:
        result.findings.append(
            "TransferHook program is present. Existence is not evidence that the hook is malicious."
        )
    if result.permanent_delegate:
        result.findings.append("PermanentDelegate is present. This is control evidence, not a classification.")
    if result.default_account_state:
        result.findings.append(f"DefaultAccountState={result.default_account_state}.")
    if "nonTransferable" in result.extension_types:
        result.findings.append("NonTransferable extension is present. This is not a standalone unsellable verdict.")
    if result.pausable_authority or result.pausable_paused is not None:
        result.findings.append("Pausable configuration is present. Evidence only.")
    if result.mint_close_authority:
        result.findings.append("MintCloseAuthority is present.")
    if result.confidential_transfer_present:
        result.findings.append("ConfidentialTransferMint is present.")
    unknown = [name for name in result.extension_types if name not in KNOWN_EXTENSION_TYPES]
    if unknown:
        result.findings.append(
            "Additional extension type(s) preserved without security classification: " + ", ".join(unknown)
        )


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep JSON-serializable public fields; drop oversized opaque blobs."""
    out: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 512:
                out[key] = value[:512] + "…"
            else:
                out[key] = value
        elif isinstance(value, dict):
            out[key] = _safe_state(value)
        elif isinstance(value, list):
            out[key] = [item if isinstance(item, (str, int, float, bool)) or item is None else str(type(item)) for item in value[:32]]
    return out


def _pubkey(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and value.strip().lower() not in {"none", "null"}:
        return value.strip()
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None
