"""Explainable sellability and control-risk findings from stored observations.

Derived at query time. Presence of an extension or authority is not a scam,
rug, or honeypot label and does not change opportunity scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Token, Token2022ExtensionObservation, TokenSecurityScan
from app.services.opportunity_scoring import observation_time
from app.services.solana_pda import is_valid_pubkey
from app.services.token_2022_extensions import (
    KNOWN_EXTENSION_TYPES,
    STATUS_AVAILABLE,
    STATUS_NO_EXTENSIONS,
    STATUS_UNAVAILABLE,
    STATUS_UNSUPPORTED,
    latest_observation_as_of,
)
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from app.timeutil import as_utc, utc_now

VERIFIED_PRESENT = "VERIFIED_PRESENT"
VERIFIED_ABSENT = "VERIFIED_ABSENT"
UNKNOWN = "UNKNOWN"
UNAVAILABLE = "UNAVAILABLE"

INFORMATIONAL = "INFORMATIONAL"
CONTROL_RISK = "CONTROL_RISK"
TRANSFER_RESTRICTION = "TRANSFER_RESTRICTION"
SELLABILITY_CONCERN = "SELLABILITY_CONCERN"

TRANSFER_FEE_CONFIGURED = "TRANSFER_FEE_CONFIGURED"
TRANSFER_FEE_AUTHORITY_PRESENT = "TRANSFER_FEE_AUTHORITY_PRESENT"
TRANSFER_HOOK_CONFIGURED = "TRANSFER_HOOK_CONFIGURED"
PERMANENT_DELEGATE_PRESENT = "PERMANENT_DELEGATE_PRESENT"
DEFAULT_ACCOUNT_FROZEN = "DEFAULT_ACCOUNT_FROZEN"
NON_TRANSFERABLE = "NON_TRANSFERABLE"
PAUSABLE_TOKEN = "PAUSABLE_TOKEN"
MINT_CLOSE_AUTHORITY_PRESENT = "MINT_CLOSE_AUTHORITY_PRESENT"
CONFIDENTIAL_TRANSFER_ENABLED = "CONFIDENTIAL_TRANSFER_ENABLED"
METADATA_POINTER_PRESENT = "METADATA_POINTER_PRESENT"
TOKEN_METADATA_PRESENT = "TOKEN_METADATA_PRESENT"
SCALED_UI_AMOUNT_CONFIGURED = "SCALED_UI_AMOUNT_CONFIGURED"
MINT_AUTHORITY_ACTIVE = "MINT_AUTHORITY_ACTIVE"
FREEZE_AUTHORITY_ACTIVE = "FREEZE_AUTHORITY_ACTIVE"
UNKNOWN_EXTENSION_PRESENT = "UNKNOWN_EXTENSION_PRESENT"

MAX_FEE_BASIS_POINTS = 10_000

T22_CODES = (
    TRANSFER_FEE_CONFIGURED,
    TRANSFER_FEE_AUTHORITY_PRESENT,
    TRANSFER_HOOK_CONFIGURED,
    PERMANENT_DELEGATE_PRESENT,
    DEFAULT_ACCOUNT_FROZEN,
    NON_TRANSFERABLE,
    PAUSABLE_TOKEN,
    MINT_CLOSE_AUTHORITY_PRESENT,
    CONFIDENTIAL_TRANSFER_ENABLED,
    METADATA_POINTER_PRESENT,
    TOKEN_METADATA_PRESENT,
    SCALED_UI_AMOUNT_CONFIGURED,
)

AUTHORITY_CODES = (MINT_AUTHORITY_ACTIVE, FREEZE_AUTHORITY_ACTIVE)

_EXTENSION_BY_CODE = {
    TRANSFER_FEE_CONFIGURED: ("transferFeeConfig",),
    TRANSFER_FEE_AUTHORITY_PRESENT: ("transferFeeConfig",),
    TRANSFER_HOOK_CONFIGURED: ("transferHook",),
    PERMANENT_DELEGATE_PRESENT: ("permanentDelegate",),
    DEFAULT_ACCOUNT_FROZEN: ("defaultAccountState",),
    NON_TRANSFERABLE: ("nonTransferable",),
    PAUSABLE_TOKEN: ("pausable", "pausableConfig"),
    MINT_CLOSE_AUTHORITY_PRESENT: ("mintCloseAuthority",),
    CONFIDENTIAL_TRANSFER_ENABLED: ("confidentialTransferMint",),
    METADATA_POINTER_PRESENT: ("metadataPointer",),
    TOKEN_METADATA_PRESENT: ("tokenMetadata",),
    SCALED_UI_AMOUNT_CONFIGURED: ("scaledUiAmount", "scaledUiAmountConfig"),
}


@dataclass
class ControlFinding:
    code: str
    severity: str
    status: str
    explanation: str
    evidence: dict[str, Any] = field(default_factory=dict)
    source_observation_id: int | None = None
    source_observation_kind: str | None = None
    source: str | None = None
    observed_at: datetime | None = None
    slot: int | None = None
    confidence: float = 0.0
    limitations: str = ""
    verified: bool = False


@dataclass
class ControlFindingsReport:
    token_address: str
    signal_time: datetime
    findings: list[ControlFinding]
    note: str = (
        "Explainable evidence only. Findings are not a scam, rug, or honeypot label "
        "and do not change opportunity scores."
    )


def findings_for_token(
    session: Session,
    token: Token,
    signal_time: datetime | None = None,
) -> ControlFindingsReport:
    as_of = as_utc(signal_time) or utc_now()
    t22 = latest_observation_as_of(session, token.id, as_of)
    security = latest_security_scan_as_of(session, token.id, as_of)
    findings = evaluate_control_findings(
        token.token_address,
        t22_observation=t22,
        security_scan=security,
        signal_time=as_of,
    )
    return ControlFindingsReport(token_address=token.token_address, signal_time=as_of, findings=findings)


def latest_security_scan_as_of(
    session: Session,
    token_id: int,
    as_of: datetime,
) -> TokenSecurityScan | None:
    cutoff = as_utc(as_of)
    rows = list(
        session.scalars(
            select(TokenSecurityScan)
            .where(TokenSecurityScan.token_id == token_id)
            .order_by(TokenSecurityScan.id.desc())
        ).all()
    )
    eligible = []
    for row in rows:
        stamp = observation_time(row)
        if stamp is None or cutoff is None or stamp > cutoff:
            continue
        eligible.append(row)
    if not eligible:
        return None
    eligible.sort(key=lambda item: observation_time(item) or cutoff, reverse=True)
    return eligible[0]


def evaluate_control_findings(
    requested_mint: str,
    *,
    t22_observation: Token2022ExtensionObservation | None,
    security_scan: TokenSecurityScan | None,
    signal_time: datetime,
) -> list[ControlFinding]:
    findings: list[ControlFinding] = []
    findings.extend(_token_2022_findings(requested_mint, t22_observation, signal_time))
    findings.extend(_authority_findings(requested_mint, security_scan, signal_time))
    return findings


def finding_by_code(findings: list[ControlFinding], code: str) -> ControlFinding | None:
    for item in findings:
        if item.code == code:
            return item
    return None


def _token_2022_findings(
    requested_mint: str,
    obs: Token2022ExtensionObservation | None,
    signal_time: datetime,
) -> list[ControlFinding]:
    if obs is None:
        return [_blank(code, UNKNOWN, "No Token-2022 observation at or before signal time.") for code in T22_CODES]

    provenance = _t22_provenance(obs)
    if as_utc(obs.observed_at) is None:
        return [
            _finding(
                code,
                INFORMATIONAL,
                UNAVAILABLE,
                "Observation has no successful observed_at and cannot be used.",
                provenance=provenance,
                limitations="A failed or untimestamped RPC row is not historical evidence.",
            )
            for code in T22_CODES
        ]
    if as_utc(obs.observed_at) and as_utc(obs.observed_at) > as_utc(signal_time):  # type: ignore[operator]
        return [_blank(code, UNKNOWN, "Later observation excluded from point-in-time view.") for code in T22_CODES]
    if obs.mint_address != requested_mint:
        return [
            _finding(
                code,
                INFORMATIONAL,
                UNKNOWN,
                "Stored observation mint does not match the requested token mint.",
                evidence={"requested_mint": requested_mint, "observation_mint": obs.mint_address},
                provenance=provenance,
                limitations="A correctly parsed account for a different mint is never attached to this token.",
            )
            for code in T22_CODES
        ]

    owner = obs.token_program
    if owner and owner not in {TOKEN_2022_PROGRAM, TOKEN_PROGRAM}:
        return [
            _finding(
                code,
                INFORMATIONAL,
                UNAVAILABLE,
                "Account owner is not a supported token program.",
                evidence={"token_program": owner},
                provenance=provenance,
                limitations="Wrong owner prevents Token-2022 and SPL mint conclusions.",
            )
            for code in T22_CODES
        ]

    types = _extension_types(obs)
    parser_complete = bool(obs.parser_complete)
    status = obs.inspection_status

    if status == STATUS_UNSUPPORTED and owner == TOKEN_PROGRAM and parser_complete:
        return [
            _absent(
                code,
                provenance,
                "Classic SPL Token mint cannot host Token-2022 mint extensions.",
                evidence={"token_program": owner, "inspection_status": status},
                limitations="Verified absent for Token-2022 extensions only. This is not a safety rating.",
            )
            for code in T22_CODES
        ]

    if status == STATUS_UNSUPPORTED:
        return [
            _finding(
                code,
                INFORMATIONAL,
                UNAVAILABLE,
                "Token-2022 extension representation is outside supported inspection.",
                evidence={"inspection_status": status, "parser_notes": obs.parser_notes},
                provenance=provenance,
                limitations=obs.parser_notes or "Unsupported representation is not treated as safe.",
            )
            for code in T22_CODES
        ]

    if status == STATUS_UNAVAILABLE or not parser_complete:
        present_unknown = [
            _token_2022_code_finding(code, obs, types, complete=False, provenance=provenance) for code in T22_CODES
        ]
        extra = _unknown_extension_finding(types, provenance, complete=False)
        return present_unknown + extra

    if status == STATUS_NO_EXTENSIONS and parser_complete:
        rows = [
            _absent(
                code,
                provenance,
                "Complete jsonParsed Token-2022 inspection reported no extensions.",
                evidence={"inspection_status": status, "extension_types": types},
                limitations="Verified absence of known extensions is not proof the token is safe.",
            )
            for code in T22_CODES
        ]
        extra = _unknown_extension_finding(types, provenance, complete=True)
        return rows + extra

    rows = [_token_2022_code_finding(code, obs, types, complete=True, provenance=provenance) for code in T22_CODES]
    return rows + _unknown_extension_finding(types, provenance, complete=True)


def _token_2022_code_finding(
    code: str,
    obs: Token2022ExtensionObservation,
    types: list[str],
    *,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    names = _EXTENSION_BY_CODE[code]
    present = any(name in types for name in names)
    if code == TRANSFER_FEE_CONFIGURED:
        return _transfer_fee_configured(obs, types, complete, provenance, present)
    if code == TRANSFER_FEE_AUTHORITY_PRESENT:
        return _transfer_fee_authority(obs, types, complete, provenance, present)
    if code == TRANSFER_HOOK_CONFIGURED:
        return _transfer_hook(obs, types, complete, provenance, present)
    if code == PERMANENT_DELEGATE_PRESENT:
        return _pubkey_extension_finding(
            code,
            CONTROL_RISK,
            obs.permanent_delegate,
            present,
            complete,
            provenance,
            present_explanation="A permanent delegate is configured. This is a privileged control, not proof of theft.",
            absent_explanation="No permanentDelegate extension in the complete inspection.",
            invalid_explanation="Permanent delegate value is not a valid public key.",
            evidence_key="permanent_delegate",
            limitations="Delegate capability is not a theft or scam classification.",
        )
    if code == DEFAULT_ACCOUNT_FROZEN:
        return _default_frozen(obs, present, complete, provenance)
    if code == NON_TRANSFERABLE:
        if not complete:
            return _incomplete_presence(code, SELLABILITY_CONCERN, present, provenance, "nonTransferable")
        if present:
            return _finding(
                code,
                SELLABILITY_CONCERN,
                VERIFIED_PRESENT,
                "NonTransferable is decoded. This is a verified transfer restriction, not a standalone unsellable verdict.",
                evidence={"extension": "nonTransferable"},
                provenance=provenance,
                confidence=1.0,
                verified=True,
                limitations="Does not by itself prove a honeypot or that every market path is blocked.",
            )
        return _absent(code, provenance, "No nonTransferable extension in the complete inspection.")
    if code == PAUSABLE_TOKEN:
        return _pausable(obs, present, complete, provenance)
    if code == MINT_CLOSE_AUTHORITY_PRESENT:
        return _pubkey_extension_finding(
            code,
            CONTROL_RISK,
            obs.mint_close_authority,
            present,
            complete,
            provenance,
            present_explanation="Mint close authority is configured. This is a privilege, not proof it will be used.",
            absent_explanation="No mintCloseAuthority extension in the complete inspection.",
            invalid_explanation="Mint close authority is not a valid public key.",
            evidence_key="mint_close_authority",
            limitations="Close authority is control evidence only.",
            allow_present_without_key=True,
        )
    if code == CONFIDENTIAL_TRANSFER_ENABLED:
        return _confidential(obs, present, complete, provenance)
    if code == METADATA_POINTER_PRESENT:
        return _metadata_pointer(obs, present, complete, provenance)
    if code == TOKEN_METADATA_PRESENT:
        return _token_metadata(obs, present, complete, provenance)
    if code == SCALED_UI_AMOUNT_CONFIGURED:
        return _scaled_ui(obs, present, complete, provenance)
    return _blank(code, UNKNOWN, "Unhandled finding code.")


def _transfer_fee_configured(
    obs: Token2022ExtensionObservation,
    types: list[str],
    complete: bool,
    provenance: dict[str, Any],
    present: bool,
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(TRANSFER_FEE_CONFIGURED, TRANSFER_RESTRICTION, present, provenance, "transferFeeConfig")
    if not present:
        return _absent(TRANSFER_FEE_CONFIGURED, provenance, "No transferFeeConfig extension in the complete inspection.")
    bps = obs.transfer_fee_bps
    if bps is not None and not isinstance(bps, int):
        return _finding(
            TRANSFER_FEE_CONFIGURED,
            TRANSFER_RESTRICTION,
            UNKNOWN,
            "Transfer fee basis points have an invalid type.",
            evidence={"transfer_fee_bps": bps},
            provenance=provenance,
            limitations="Invalid numeric types are not treated as a configured fee.",
        )
    if bps is None:
        return _finding(
            TRANSFER_FEE_CONFIGURED,
            TRANSFER_RESTRICTION,
            UNKNOWN,
            "TransferFeeConfig is listed but basis points were not a usable integer.",
            evidence={"extension_types": types, "transfer_fee_max": obs.transfer_fee_max},
            provenance=provenance,
            limitations="Missing fee values are not defaulted to zero.",
        )
    if bps < 0 or bps > MAX_FEE_BASIS_POINTS:
        return _finding(
            TRANSFER_FEE_CONFIGURED,
            TRANSFER_RESTRICTION,
            UNKNOWN,
            "Transfer fee basis points are outside the valid 0–10000 range.",
            evidence={"transfer_fee_bps": bps},
            provenance=provenance,
            limitations="Out-of-range values are not treated as a verified fee.",
        )
    severity = TRANSFER_RESTRICTION if bps > 0 else INFORMATIONAL
    return _finding(
        TRANSFER_FEE_CONFIGURED,
        severity,
        VERIFIED_PRESENT,
        f"TransferFeeConfig is present with {bps} basis points. A transfer fee is not automatically malicious.",
        evidence={"transfer_fee_bps": bps, "transfer_fee_max": obs.transfer_fee_max},
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Fee configuration is not a scam label.",
    )


def _transfer_fee_authority(
    obs: Token2022ExtensionObservation,
    types: list[str],
    complete: bool,
    provenance: dict[str, Any],
    present: bool,
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(TRANSFER_FEE_AUTHORITY_PRESENT, CONTROL_RISK, present, provenance, "transferFeeConfig")
    if not present:
        return _absent(TRANSFER_FEE_AUTHORITY_PRESENT, provenance, "No transferFeeConfig, so no transfer-fee authorities.")
    config_auth, config_status = _authority_value(obs.transfer_fee_config_authority)
    withdraw_auth, withdraw_status = _authority_value(obs.transfer_fee_withdraw_authority)
    if config_status == UNKNOWN or withdraw_status == UNKNOWN:
        return _finding(
            TRANSFER_FEE_AUTHORITY_PRESENT,
            CONTROL_RISK,
            UNKNOWN,
            "Transfer-fee authority field is not a valid public key or explicit null.",
            evidence={
                "transfer_fee_config_authority": obs.transfer_fee_config_authority,
                "transfer_fee_withdraw_authority": obs.transfer_fee_withdraw_authority,
            },
            provenance=provenance,
            limitations="Invalid keys are not treated as active authorities.",
        )
    if config_status == VERIFIED_PRESENT or withdraw_status == VERIFIED_PRESENT:
        return _finding(
            TRANSFER_FEE_AUTHORITY_PRESENT,
            CONTROL_RISK,
            VERIFIED_PRESENT,
            "A transfer-fee configuration or withdrawal authority is present. This is a privilege, not proof of abuse.",
            evidence={
                "transfer_fee_config_authority": config_auth,
                "transfer_fee_withdraw_authority": withdraw_auth,
            },
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations="Authority presence is not a scam classification.",
        )
    return _finding(
        TRANSFER_FEE_AUTHORITY_PRESENT,
        CONTROL_RISK,
        VERIFIED_ABSENT,
        "TransferFeeConfig is present and both fee authorities are explicitly null.",
        evidence={
            "transfer_fee_config_authority": None,
            "transfer_fee_withdraw_authority": None,
        },
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Revoked fee authorities are not a safety rating.",
    )


def _transfer_hook(
    obs: Token2022ExtensionObservation,
    types: list[str],
    complete: bool,
    provenance: dict[str, Any],
    present: bool,
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(TRANSFER_HOOK_CONFIGURED, SELLABILITY_CONCERN, present, provenance, "transferHook")
    if not present:
        return _absent(TRANSFER_HOOK_CONFIGURED, provenance, "No transferHook extension in the complete inspection.")
    program, program_status = _authority_value(obs.transfer_hook_program_id)
    if program_status == UNKNOWN:
        return _finding(
            TRANSFER_HOOK_CONFIGURED,
            SELLABILITY_CONCERN,
            UNKNOWN,
            "Transfer hook program id is not a valid public key.",
            evidence={"transfer_hook_program_id": obs.transfer_hook_program_id},
            provenance=provenance,
            limitations="Invalid program ids are not treated as a configured hook.",
        )
    if program_status != VERIFIED_PRESENT:
        return _finding(
            TRANSFER_HOOK_CONFIGURED,
            SELLABILITY_CONCERN,
            UNKNOWN,
            "TransferHook is listed but no valid hook program id was decoded.",
            evidence={"extension_types": types, "transfer_hook_authority": obs.transfer_hook_authority},
            provenance=provenance,
            limitations="A configured hook is not inferred from a missing program id.",
        )
    return _finding(
        TRANSFER_HOOK_CONFIGURED,
        SELLABILITY_CONCERN,
        VERIFIED_PRESENT,
        "A transfer hook program is configured. This does not prove malicious behavior.",
        evidence={
            "transfer_hook_program_id": program,
            "transfer_hook_authority": obs.transfer_hook_authority if _authority_value(obs.transfer_hook_authority)[1] != UNKNOWN else None,
        },
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Hook existence is not evidence that transfers are blocked or that the program is malicious.",
    )


def _default_frozen(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(DEFAULT_ACCOUNT_FROZEN, TRANSFER_RESTRICTION, present, provenance, "defaultAccountState")
    if not present:
        return _absent(DEFAULT_ACCOUNT_FROZEN, provenance, "No defaultAccountState extension in the complete inspection.")
    raw = obs.default_account_state
    if raw is None or not isinstance(raw, str):
        return _finding(
            DEFAULT_ACCOUNT_FROZEN,
            TRANSFER_RESTRICTION,
            UNKNOWN,
            "Default account state is missing or not a string.",
            evidence={"default_account_state": raw},
            provenance=provenance,
            limitations="No default frozen claim is made from missing state.",
        )
    normalized = raw.strip().lower()
    if normalized == "frozen":
        return _finding(
            DEFAULT_ACCOUNT_FROZEN,
            SELLABILITY_CONCERN,
            VERIFIED_PRESENT,
            "Default account state is frozen. This is a significant transfer restriction.",
            evidence={"default_account_state": raw},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations=(
                "Thaw depends on freeze-authority evidence from the mint. "
                "This finding does not claim accounts cannot be thawed and is not a honeypot label."
            ),
        )
    if normalized in {"initialized", "uninitialized"}:
        return _finding(
            DEFAULT_ACCOUNT_FROZEN,
            TRANSFER_RESTRICTION,
            VERIFIED_ABSENT,
            f"Default account state is {normalized}, not frozen.",
            evidence={"default_account_state": raw},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations="A non-frozen default is not a safety rating.",
        )
    return _finding(
        DEFAULT_ACCOUNT_FROZEN,
        TRANSFER_RESTRICTION,
        UNKNOWN,
        "Default account state value is not a recognized account state.",
        evidence={"default_account_state": raw},
        provenance=provenance,
        limitations="Unrecognized state strings are not classified as frozen or thawed.",
    )


def _pausable(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(PAUSABLE_TOKEN, CONTROL_RISK, present, provenance, "pausable")
    if not present:
        return _absent(PAUSABLE_TOKEN, provenance, "No pausable extension in the complete inspection.")
    auth, auth_status = _authority_value(obs.pausable_authority)
    paused = obs.pausable_paused
    if paused is not None and paused not in (0, 1, True, False):
        return _finding(
            PAUSABLE_TOKEN,
            CONTROL_RISK,
            UNKNOWN,
            "Pausable paused flag has an invalid type.",
            evidence={"pausable_paused": paused},
            provenance=provenance,
        )
    paused_bool = None if paused is None else bool(paused)
    explanation = "A pausable extension is present, indicating a pause capability."
    if paused_bool is True:
        explanation = "Pausable extension is present and the paused flag is true on this observation."
    elif paused_bool is False:
        explanation += " The paused flag is false; transfers are not claimed to be paused."
    else:
        explanation += " Current paused state was not decoded; transfers are not claimed to be paused."
    if auth_status == UNKNOWN:
        return _finding(
            PAUSABLE_TOKEN,
            CONTROL_RISK,
            UNKNOWN,
            "Pausable authority is not a valid public key or explicit null.",
            evidence={"pausable_authority": obs.pausable_authority, "pausable_paused": paused_bool},
            provenance=provenance,
        )
    return _finding(
        PAUSABLE_TOKEN,
        CONTROL_RISK if paused_bool is not True else TRANSFER_RESTRICTION,
        VERIFIED_PRESENT,
        explanation,
        evidence={"pausable_authority": auth, "pausable_paused": paused_bool},
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Pause capability is not proof that transfers are currently paused unless the paused flag is true.",
    )


def _confidential(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(CONFIDENTIAL_TRANSFER_ENABLED, INFORMATIONAL, present, provenance, "confidentialTransferMint")
    if not present and not obs.confidential_transfer_present:
        return _absent(CONFIDENTIAL_TRANSFER_ENABLED, provenance, "No confidentialTransferMint extension in the complete inspection.")
    return _finding(
        CONFIDENTIAL_TRANSFER_ENABLED,
        INFORMATIONAL,
        VERIFIED_PRESENT,
        "ConfidentialTransferMint is configured. Holder analysis may be limited; hidden balances are not inferred.",
        evidence={
            "confidential_transfer_present": True,
            "confidential_auto_approve": None if obs.confidential_auto_approve is None else bool(obs.confidential_auto_approve),
        },
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Confidential configuration is not evidence of hidden or stolen balances.",
    )


def _metadata_pointer(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(METADATA_POINTER_PRESENT, INFORMATIONAL, present, provenance, "metadataPointer")
    if not present:
        return _absent(METADATA_POINTER_PRESENT, provenance, "No metadataPointer extension in the complete inspection.")
    pointer, pointer_status = _authority_value(obs.metadata_pointer_address)
    if obs.metadata_pointer_address is not None and pointer_status == UNKNOWN:
        return _finding(
            METADATA_POINTER_PRESENT,
            INFORMATIONAL,
            UNKNOWN,
            "Metadata pointer address is not a valid public key.",
            evidence={"metadata_pointer_address": obs.metadata_pointer_address},
            provenance=provenance,
        )
    return _finding(
        METADATA_POINTER_PRESENT,
        INFORMATIONAL,
        VERIFIED_PRESENT,
        "Metadata pointer is present. This does not prove metadata is immutable or trustworthy.",
        evidence={
            "metadata_pointer_address": pointer,
            "metadata_pointer_authority": obs.metadata_pointer_authority,
        },
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="Pointer presence is not a metadata integrity guarantee.",
    )


def _token_metadata(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(TOKEN_METADATA_PRESENT, INFORMATIONAL, present, provenance, "tokenMetadata")
    if not present and not obs.token_metadata_present:
        return _absent(TOKEN_METADATA_PRESENT, provenance, "No tokenMetadata extension in the complete inspection.")
    fields = _parsed_fields(obs).get("tokenMetadata")
    if isinstance(fields, dict):
        meta_mint = fields.get("mint")
        if isinstance(meta_mint, str) and meta_mint.strip() and meta_mint.strip() != obs.mint_address:
            return _finding(
                TOKEN_METADATA_PRESENT,
                INFORMATIONAL,
                UNKNOWN,
                "TokenMetadata mint does not match the requested mint.",
                evidence={"token_metadata_mint": meta_mint, "requested_mint": obs.mint_address},
                provenance=provenance,
                limitations="Mismatched metadata mint is never treated as this token's metadata.",
            )
        if meta_mint is not None and not is_valid_pubkey(meta_mint) and meta_mint != "":
            return _finding(
                TOKEN_METADATA_PRESENT,
                INFORMATIONAL,
                UNKNOWN,
                "TokenMetadata mint is not a valid public key.",
                evidence={"token_metadata_mint": meta_mint},
                provenance=provenance,
            )
    return _finding(
        TOKEN_METADATA_PRESENT,
        INFORMATIONAL,
        VERIFIED_PRESENT,
        "TokenMetadata extension is present. This does not prove metadata is immutable or trustworthy.",
        evidence={"token_metadata_present": True},
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="On-chain TokenMetadata is not a trust rating.",
    )


def _scaled_ui(
    obs: Token2022ExtensionObservation,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(SCALED_UI_AMOUNT_CONFIGURED, INFORMATIONAL, present, provenance, "scaledUiAmount")
    if not present:
        return _absent(SCALED_UI_AMOUNT_CONFIGURED, provenance, "No scaled UI amount extension in the complete inspection.")
    multiplier = obs.scaled_ui_amount_multiplier
    if multiplier is not None and not isinstance(multiplier, str):
        return _finding(
            SCALED_UI_AMOUNT_CONFIGURED,
            INFORMATIONAL,
            UNKNOWN,
            "Scaled UI amount multiplier has an invalid type.",
            evidence={"scaled_ui_amount_multiplier": multiplier},
            provenance=provenance,
        )
    return _finding(
        SCALED_UI_AMOUNT_CONFIGURED,
        INFORMATIONAL,
        VERIFIED_PRESENT,
        "ScaledUiAmount is configured. Display amounts may differ from raw token amounts.",
        evidence={"scaled_ui_amount_multiplier": multiplier},
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations="UI scaling is not a transfer-block or scam label.",
    )


def _pubkey_extension_finding(
    code: str,
    severity: str,
    value: str | None,
    present: bool,
    complete: bool,
    provenance: dict[str, Any],
    *,
    present_explanation: str,
    absent_explanation: str,
    invalid_explanation: str,
    evidence_key: str,
    limitations: str,
    allow_present_without_key: bool = False,
) -> ControlFinding:
    if not complete:
        return _incomplete_presence(code, severity, present, provenance, evidence_key)
    if not present:
        return _absent(code, provenance, absent_explanation)
    key, key_status = _authority_value(value)
    if key_status == UNKNOWN:
        return _finding(
            code,
            severity,
            UNKNOWN,
            invalid_explanation,
            evidence={evidence_key: value},
            provenance=provenance,
            limitations=limitations,
        )
    if key_status == VERIFIED_PRESENT:
        return _finding(
            code,
            severity,
            VERIFIED_PRESENT,
            present_explanation,
            evidence={evidence_key: key},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations=limitations,
        )
    if allow_present_without_key:
        return _finding(
            code,
            severity,
            VERIFIED_PRESENT,
            present_explanation + " The authority field is explicitly null.",
            evidence={evidence_key: None},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations=limitations,
        )
    return _finding(
        code,
        severity,
        UNKNOWN,
        "Extension is listed but the authority/delegate was not a usable public key.",
        evidence={evidence_key: value},
        provenance=provenance,
        limitations=limitations,
    )


def _unknown_extension_finding(
    types: list[str],
    provenance: dict[str, Any],
    *,
    complete: bool,
) -> list[ControlFinding]:
    unknown = [name for name in types if name not in KNOWN_EXTENSION_TYPES]
    if not unknown:
        return []
    status = VERIFIED_PRESENT if complete else UNKNOWN
    return [
        _finding(
            UNKNOWN_EXTENSION_PRESENT,
            INFORMATIONAL,
            status,
            "Additional extension type(s) were returned and are preserved without a clean classification.",
            evidence={"extension_types": unknown},
            provenance=provenance,
            confidence=1.0 if complete else 0.0,
            verified=complete,
            limitations="Unknown extension names are not treated as safe or unsafe.",
        )
    ]


def _authority_findings(
    requested_mint: str,
    scan: TokenSecurityScan | None,
    signal_time: datetime,
) -> list[ControlFinding]:
    if scan is None:
        return [
            _blank(MINT_AUTHORITY_ACTIVE, UNKNOWN, "No mint security observation at or before signal time."),
            _blank(FREEZE_AUTHORITY_ACTIVE, UNKNOWN, "No mint security observation at or before signal time."),
        ]
    stamp = observation_time(scan)
    if stamp is not None and as_utc(signal_time) is not None and stamp > as_utc(signal_time):
        return [
            _blank(MINT_AUTHORITY_ACTIVE, UNKNOWN, "Later mint security observation excluded from point-in-time view."),
            _blank(FREEZE_AUTHORITY_ACTIVE, UNKNOWN, "Later mint security observation excluded from point-in-time view."),
        ]
    provenance = {
        "source_observation_id": scan.id,
        "source_observation_kind": "token_security_scan",
        "source": scan.source or scan.provider,
        "observed_at": scan.observed_at or scan.scanned_at,
        "slot": scan.slot,
    }
    return [
        _mint_or_freeze(
            MINT_AUTHORITY_ACTIVE,
            scan.mint_authority,
            scan.mint_authority_status,
            provenance,
            present="Mint authority is active on-chain. This is a verified privilege, not proof it will be abused.",
            absent="Mint authority is null on-chain for this observation.",
            unknown="Mint authority status is not verified on this observation.",
        ),
        _mint_or_freeze(
            FREEZE_AUTHORITY_ACTIVE,
            scan.freeze_authority,
            scan.freeze_authority_status,
            provenance,
            present="Freeze authority is active on-chain. This is a verified privilege, not proof it will be abused.",
            absent="Freeze authority is null on-chain for this observation.",
            unknown="Freeze authority status is not verified on this observation.",
        ),
    ]


def _mint_or_freeze(
    code: str,
    value: str | None,
    status: str,
    provenance: dict[str, Any],
    *,
    present: str,
    absent: str,
    unknown: str,
) -> ControlFinding:
    if status == "VERIFIED_PRESENT":
        key, key_status = _authority_value(value)
        if key_status != VERIFIED_PRESENT:
            return _finding(
                code,
                CONTROL_RISK,
                UNKNOWN,
                "Authority marked present but the stored key is not a valid public key.",
                evidence={"authority": value, "authority_status": status},
                provenance=provenance,
                limitations="Invalid keys are not treated as active authorities.",
            )
        return _finding(
            code,
            CONTROL_RISK,
            VERIFIED_PRESENT,
            present,
            evidence={"authority": key, "authority_status": status},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations="Privilege presence is not a scam or rug classification.",
        )
    if status == "VERIFIED_REVOKED":
        return _finding(
            code,
            CONTROL_RISK,
            VERIFIED_ABSENT,
            absent,
            evidence={"authority": None, "authority_status": status},
            provenance=provenance,
            confidence=1.0,
            verified=True,
            limitations="Revoked authority is not a safety rating.",
        )
    return _finding(
        code,
        CONTROL_RISK,
        UNKNOWN,
        unknown,
        evidence={"authority": value, "authority_status": status},
        provenance=provenance,
        limitations="Unknown authority status is not treated as revoked or safe.",
    )


def _incomplete_presence(
    code: str,
    severity: str,
    present: bool,
    provenance: dict[str, Any],
    extension: str,
) -> ControlFinding:
    if present:
        return _finding(
            code,
            severity,
            UNKNOWN,
            f"{extension} appears in an incomplete inspection and is not treated as verified.",
            evidence={"extension": extension, "listed": True},
            provenance=provenance,
            limitations="Incomplete parser output cannot verify presence or absence.",
        )
    return _finding(
        code,
        severity,
        UNAVAILABLE if provenance else UNKNOWN,
        "Inspection is incomplete; absence of this extension is not verified.",
        evidence={"extension": extension, "listed": False},
        provenance=provenance,
        limitations="Missing or partial extension data is not treated as safe.",
        status_override=UNAVAILABLE,
    )


def _absent(
    code: str,
    provenance: dict[str, Any],
    explanation: str,
    *,
    evidence: dict[str, Any] | None = None,
    limitations: str = "Verified absence of this control is not proof of safety.",
) -> ControlFinding:
    severity = {
        TRANSFER_FEE_CONFIGURED: TRANSFER_RESTRICTION,
        TRANSFER_FEE_AUTHORITY_PRESENT: CONTROL_RISK,
        TRANSFER_HOOK_CONFIGURED: SELLABILITY_CONCERN,
        PERMANENT_DELEGATE_PRESENT: CONTROL_RISK,
        DEFAULT_ACCOUNT_FROZEN: SELLABILITY_CONCERN,
        NON_TRANSFERABLE: SELLABILITY_CONCERN,
        PAUSABLE_TOKEN: CONTROL_RISK,
        MINT_CLOSE_AUTHORITY_PRESENT: CONTROL_RISK,
        CONFIDENTIAL_TRANSFER_ENABLED: INFORMATIONAL,
        METADATA_POINTER_PRESENT: INFORMATIONAL,
        TOKEN_METADATA_PRESENT: INFORMATIONAL,
        SCALED_UI_AMOUNT_CONFIGURED: INFORMATIONAL,
        MINT_AUTHORITY_ACTIVE: CONTROL_RISK,
        FREEZE_AUTHORITY_ACTIVE: CONTROL_RISK,
    }.get(code, INFORMATIONAL)
    return _finding(
        code,
        severity,
        VERIFIED_ABSENT,
        explanation,
        evidence=evidence or {},
        provenance=provenance,
        confidence=1.0,
        verified=True,
        limitations=limitations,
    )


def _blank(code: str, status: str, explanation: str) -> ControlFinding:
    return ControlFinding(
        code=code,
        severity=INFORMATIONAL,
        status=status,
        explanation=explanation,
        confidence=0.0,
        verified=False,
        limitations="No qualifying observation was available at signal time.",
    )


def _finding(
    code: str,
    severity: str,
    status: str,
    explanation: str,
    *,
    evidence: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    confidence: float = 0.0,
    verified: bool = False,
    limitations: str = "",
    status_override: str | None = None,
) -> ControlFinding:
    prov = provenance or {}
    final_status = status_override or status
    return ControlFinding(
        code=code,
        severity=severity,
        status=final_status,
        explanation=explanation,
        evidence=evidence or {},
        source_observation_id=prov.get("source_observation_id"),
        source_observation_kind=prov.get("source_observation_kind"),
        source=prov.get("source"),
        observed_at=prov.get("observed_at"),
        slot=prov.get("slot"),
        confidence=confidence,
        limitations=limitations,
        verified=verified and final_status in {VERIFIED_PRESENT, VERIFIED_ABSENT},
    )


def _t22_provenance(obs: Token2022ExtensionObservation) -> dict[str, Any]:
    return {
        "source_observation_id": obs.id,
        "source_observation_kind": "token_2022_extension_observation",
        "source": obs.source,
        "observed_at": obs.observed_at,
        "slot": obs.slot,
    }


def _extension_types(obs: Token2022ExtensionObservation) -> list[str]:
    if not obs.extension_types_json:
        return []
    try:
        loaded = json.loads(obs.extension_types_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if isinstance(item, str)]


def _parsed_fields(obs: Token2022ExtensionObservation) -> dict[str, Any]:
    if not obs.parsed_fields_json:
        return {}
    try:
        loaded = json.loads(obs.parsed_fields_json)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _authority_value(value: Any) -> tuple[str | None, str]:
    if value is None:
        return None, VERIFIED_ABSENT
    if not isinstance(value, str):
        return None, UNKNOWN
    stripped = value.strip()
    if stripped == "" or stripped.lower() in {"none", "null"}:
        return None, VERIFIED_ABSENT
    if is_valid_pubkey(stripped):
        return stripped, VERIFIED_PRESENT
    return None, UNKNOWN
