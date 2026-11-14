"""Mint initialization evidence from public Solana RPC. Not identity, not a deployer claim."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CreatorObservation
from app.services.rpc_accounting import KIND_RATE_LIMITED, KIND_TIMEOUT, RpcAccounting, attach_accounting
from app.services.solana_pda import is_valid_pubkey
from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from app.timeutil import as_utc, utc_now

STATUS_VERIFIED = "VERIFIED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_UNAVAILABLE = "UNAVAILABLE"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_CREATE_DISCRIMINATOR = hashlib.sha256(b"global:create").digest()[:8]
SPL_INIT_TYPES = {"initializeMint", "initializeMint2"}

SIGNATURE_PAGE_SIZE = 1000
MAX_TX_FETCH = 5
DEFAULT_SIGNATURE_PAGES = 20
SOURCE = "solana_rpc"


@dataclass
class MintInitInspection:
    mint: str
    token_program: str | None = None
    inspection_status: str = STATUS_UNKNOWN
    mint_init_signature: str | None = None
    mint_init_slot: int | None = None
    mint_init_block_time: int | None = None
    mint_init_signer: str | None = None
    mint_init_status: str = STATUS_UNKNOWN
    fee_payer: str | None = None
    signer_addresses: list[str] = field(default_factory=list)
    create_program_id: str | None = None
    create_instruction: str | None = None
    source: str = SOURCE
    observed_at: datetime | None = None
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
    signature_pages: int = 0
    signatures_retrieved: int = 0
    transactions_inspected: int = 0
    transactions_skipped: int = 0
    attempted_at: datetime | None = None
    history_incomplete: bool = False
    from_cache: bool = False


async def inspect_mint_initialization(mint: str, rpc: SolanaRpcClient) -> MintInitInspection:
    result = MintInitInspection(mint=mint.strip(), attempted_at=utc_now())
    start = rpc.accounting.copy()
    try:
        if not is_valid_pubkey(result.mint):
            result.inspection_status = STATUS_UNKNOWN
            result.mint_init_status = STATUS_UNKNOWN
            result.parser_notes = "Mint address is not a valid public key."
            result.errors.append("Invalid mint address")
            return result

        signatures, history_ok = await _fetch_signatures(result, rpc)
        if result.inspection_status == STATUS_UNAVAILABLE:
            return result
        if not history_ok:
            return result
        if not signatures:
            result.inspection_status = STATUS_UNKNOWN
            result.mint_init_status = STATUS_UNKNOWN
            result.parser_notes = (
                "Signature history for this mint was empty. Missing history is UNKNOWN, "
                "not proof that initialization is absent."
            )
            return result
        if result.history_incomplete:
            result.inspection_status = STATUS_UNKNOWN
            result.mint_init_status = STATUS_UNKNOWN
            result.transactions_skipped = len(signatures)
            result.parser_notes = (
                f"Signature history is truncated after {result.signature_pages} page(s) "
                f"({result.signatures_retrieved} signatures). Incomplete history is UNKNOWN, "
                "not VERIFIED_ABSENT, and no initialization transaction was assumed."
            )
            return result

        oldest_first = list(reversed(signatures))
        inspected = 0
        for item in oldest_first[:MAX_TX_FETCH]:
            signature = item.get("signature") if isinstance(item, dict) else None
            if not isinstance(signature, str) or not signature.strip():
                result.errors.append("Signature list contained an entry without a signature")
                continue
            inspected += 1
            parsed = await _fetch_and_parse(result, rpc, signature.strip())
            if parsed == "unavailable":
                result.transactions_inspected = inspected
                result.transactions_skipped = max(0, len(oldest_first) - inspected)
                return result
            if parsed == "matched":
                result.transactions_inspected = inspected
                result.transactions_skipped = max(0, len(oldest_first) - inspected)
                return result
            if parsed == "malformed":
                result.transactions_inspected = inspected
                result.transactions_skipped = max(0, len(oldest_first) - inspected)
                result.inspection_status = STATUS_UNKNOWN
                result.mint_init_status = STATUS_UNKNOWN
                if not result.parser_notes:
                    result.parser_notes = "A candidate transaction was malformed and could not be parsed."
                return result

        result.transactions_inspected = inspected
        result.transactions_skipped = max(0, len(oldest_first) - inspected)
        result.inspection_status = STATUS_UNKNOWN
        result.mint_init_status = STATUS_UNKNOWN
        if not result.parser_notes:
            result.parser_notes = (
                "Oldest available mint transactions did not contain a supported InitializeMint, "
                "InitializeMint2, or confidently matched Pump.fun create instruction."
            )
            if result.transactions_skipped:
                result.parser_notes += (
                    f" {result.transactions_skipped} later signature(s) were not fetched "
                    f"(bound {MAX_TX_FETCH} transactions)."
                )
        return result
    finally:
        attach_accounting(result, rpc.accounting.delta(start), rpc.last_attempt_at or result.attempted_at)


def parse_mint_initialization_transaction(
    mint: str,
    payload: Any,
    *,
    signature: str | None = None,
    observed_at: datetime | None = None,
    from_cache: bool = False,
) -> MintInitInspection:
    result = MintInitInspection(
        mint=mint.strip(),
        observed_at=observed_at,
        from_cache=from_cache,
        mint_init_signature=signature,
    )
    _apply_parsed_transaction(result, payload, signature)
    return result


def latest_observation_as_of(
    session: Session,
    token_id: int,
    as_of: datetime,
) -> CreatorObservation | None:
    cutoff = as_utc(as_of)
    rows = list(
        session.scalars(
            select(CreatorObservation)
            .where(CreatorObservation.token_id == token_id)
            .order_by(CreatorObservation.id.desc())
        ).all()
    )
    eligible: list[CreatorObservation] = []
    for row in rows:
        observed = as_utc(row.observed_at) if row.observed_at is not None else None
        if observed is None or cutoff is None or observed > cutoff:
            continue
        eligible.append(row)
    if not eligible:
        return None
    eligible.sort(key=lambda item: (as_utc(item.observed_at) or cutoff, item.id), reverse=True)
    return eligible[0]


def _signature_page_limit(rpc: SolanaRpcClient) -> int:
    try:
        return max(1, min(int(rpc.settings.creator_signature_page_limit), 50))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_SIGNATURE_PAGES


async def _fetch_signatures(
    result: MintInitInspection,
    rpc: SolanaRpcClient,
) -> tuple[list[dict[str, Any]], bool]:
    collected: list[dict[str, Any]] = []
    before: str | None = None
    page_limit = _signature_page_limit(rpc)
    try:
        for page in range(page_limit):
            call = await rpc.get_signatures_for_address(
                result.mint,
                limit=SIGNATURE_PAGE_SIZE,
                before=before,
            )
            result.from_cache = result.from_cache or call.from_cache
            result.signature_pages += 1
            if call.result is None:
                result.inspection_status = STATUS_UNAVAILABLE
                result.mint_init_status = STATUS_UNAVAILABLE
                result.parser_notes = "RPC returned null signature history. Evidence is UNAVAILABLE, not absent."
                result.errors.append("getSignaturesForAddress returned null")
                result.signatures_retrieved = len(collected)
                return [], False
            if not isinstance(call.result, list):
                result.inspection_status = STATUS_UNKNOWN
                result.mint_init_status = STATUS_UNKNOWN
                result.parser_notes = "Signature history was not a list."
                result.errors.append("Malformed getSignaturesForAddress result")
                result.signatures_retrieved = len(collected)
                return [], False
            batch = [item for item in call.result if isinstance(item, dict)]
            collected.extend(batch)
            result.signatures_retrieved = len(collected)
            if len(call.result) < SIGNATURE_PAGE_SIZE:
                result.history_incomplete = False
                return collected, True
            last_sig = batch[-1].get("signature") if batch else None
            if not isinstance(last_sig, str):
                result.history_incomplete = True
                result.inspection_status = STATUS_UNKNOWN
                result.mint_init_status = STATUS_UNKNOWN
                result.parser_notes = "Signature page could not be continued."
                result.errors.append("Incomplete signature pagination")
                return collected, False
            before = last_sig
            if page == page_limit - 1:
                result.history_incomplete = True
        result.history_incomplete = True
        return collected, True
    except SolanaRpcError as exc:
        _apply_rpc_error(result, exc, "getSignaturesForAddress")
        result.signatures_retrieved = len(collected)
        return [], False


async def _fetch_and_parse(
    result: MintInitInspection,
    rpc: SolanaRpcClient,
    signature: str,
) -> str:
    try:
        call = await rpc.get_transaction(signature)
    except SolanaRpcError as exc:
        _apply_rpc_error(result, exc, "getTransaction")
        return "unavailable"
    result.from_cache = result.from_cache or call.from_cache
    if call.result is None:
        result.inspection_status = STATUS_UNAVAILABLE
        result.mint_init_status = STATUS_UNAVAILABLE
        result.parser_notes = (
            "RPC returned no transaction body for a mint signature. "
            "This is UNAVAILABLE evidence, not proof that initialization is absent."
        )
        result.errors.append(f"getTransaction returned null for {signature}")
        return "unavailable"
    if not isinstance(call.result, dict):
        result.errors.append(f"Malformed getTransaction result for {signature}")
        return "malformed"
    snapshot = MintInitInspection(
        mint=result.mint,
        observed_at=call.observed_at,
        from_cache=call.from_cache,
        mint_init_signature=signature,
        errors=list(result.errors),
    )
    matched = _apply_parsed_transaction(snapshot, call.result, signature)
    if matched:
        preserved = {
            "signature_pages": result.signature_pages,
            "signatures_retrieved": result.signatures_retrieved,
            "history_incomplete": result.history_incomplete,
            "attempted_at": result.attempted_at,
        }
        snapshot.errors = result.errors + snapshot.errors
        result.__dict__.update(snapshot.__dict__)
        for key, value in preserved.items():
            setattr(result, key, value)
        return "matched"
    result.errors.extend(snapshot.errors)
    if snapshot.parser_notes:
        result.parser_notes = snapshot.parser_notes
    return "continue"


def _apply_parsed_transaction(result: MintInitInspection, payload: dict[str, Any], signature: str | None) -> bool:
    accounts = _account_entries(payload)
    if accounts is None:
        result.errors.append("Transaction account keys could not be parsed")
        result.inspection_status = STATUS_UNKNOWN
        result.mint_init_status = STATUS_UNKNOWN
        result.parser_notes = "Malformed transaction account keys."
        return False

    fee_payer = accounts[0]["pubkey"] if accounts else None
    signers = [item["pubkey"] for item in accounts if item.get("signer")]
    result.fee_payer = fee_payer
    result.signer_addresses = signers
    result.mint_init_signature = signature or _first_signature(payload) or result.mint_init_signature
    result.mint_init_slot = _as_int(payload.get("slot"))
    result.slot = result.mint_init_slot
    result.mint_init_block_time = _as_int(payload.get("blockTime"))

    instructions = _walk_instructions(payload, accounts)
    if instructions is None:
        result.inspection_status = STATUS_UNKNOWN
        result.mint_init_status = STATUS_UNKNOWN
        result.parser_notes = "Malformed transaction instructions."
        result.errors.append("Transaction instructions could not be parsed")
        return False

    spl_match = _find_spl_init(result.mint, instructions, accounts)
    pump_match = _find_pump_create(result.mint, instructions, accounts)
    matches = [item for item in (spl_match, pump_match) if item is not None]
    if len(matches) > 1:
        result.inspection_status = STATUS_UNKNOWN
        result.mint_init_status = STATUS_UNKNOWN
        result.parser_notes = "Multiple supported initialization instructions matched; signer is ambiguous."
        result.errors.append("Ambiguous initialization instructions")
        return True
    if not matches:
        unsupported = _unsupported_note(instructions)
        result.parser_notes = unsupported
        return False

    match = matches[0]
    result.create_program_id = match["program_id"]
    result.create_instruction = match["instruction"]
    result.token_program = match.get("token_program")
    result.mint_init_signature = signature or result.mint_init_signature
    result.observed_at = result.observed_at
    init_signer = match.get("init_signer")
    if init_signer:
        result.mint_init_signer = init_signer
        result.mint_init_status = STATUS_VERIFIED
        result.inspection_status = STATUS_VERIFIED
        if init_signer == result.fee_payer:
            result.parser_notes = (
                f"Matched {match['instruction']} on program {match['program_id']}. "
                "Initialization signer was taken from the instruction accounts, not assumed from the fee payer. "
                "This address is not identified as a person."
            )
        elif init_signer == result.mint:
            result.parser_notes = (
                f"Matched {match['instruction']}. The initialization signer is the mint keypair. "
                "This is not a person and is not treated as a deployer wallet."
            )
        else:
            result.parser_notes = (
                f"Matched {match['instruction']} on program {match['program_id']}. "
                "Initialization signer is recorded separately from the fee payer. Not a person."
            )
        return True

    result.mint_init_signer = None
    result.mint_init_status = STATUS_UNKNOWN
    result.inspection_status = STATUS_UNKNOWN
    result.parser_notes = (
        f"Matched {match['instruction']} but could not uniquely identify an initialization signer. "
        "The fee payer is not assumed to be the creator."
    )
    return True


def _find_spl_init(
    mint: str,
    instructions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for ix in instructions:
        parsed_type = ix.get("parsed_type")
        if parsed_type not in SPL_INIT_TYPES:
            continue
        info_mint = ix.get("info_mint")
        if info_mint and info_mint != mint:
            continue
        if not info_mint and mint not in ix.get("account_pubkeys", []):
            continue
        program_id = ix.get("program_id")
        if program_id not in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
            continue
        if found is not None:
            return None
        init_signer = _unique_instruction_signer(ix.get("account_pubkeys", []), accounts, mint)
        found = {
            "program_id": program_id,
            "instruction": parsed_type,
            "token_program": program_id,
            "init_signer": init_signer,
        }
    return found


def _find_pump_create(
    mint: str,
    instructions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for ix in instructions:
        if ix.get("program_id") != PUMP_PROGRAM:
            continue
        pubkeys = ix.get("account_pubkeys", [])
        if not pubkeys or pubkeys[0] != mint:
            continue
        if not _is_pump_create_data(ix.get("data")) and ix.get("parsed_type") not in {"create", "createV2"}:
            continue
        if found is not None:
            return None
        user = pubkeys[7] if len(pubkeys) > 7 else None
        init_signer = None
        if user and _is_signer(user, accounts):
            init_signer = user
        else:
            init_signer = _unique_instruction_signer(pubkeys, accounts, mint)
        token_program = None
        for pubkey in pubkeys:
            if pubkey in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
                token_program = pubkey
                break
        found = {
            "program_id": PUMP_PROGRAM,
            "instruction": "create",
            "token_program": token_program,
            "init_signer": init_signer,
        }
    return found


def _unique_instruction_signer(
    ix_accounts: list[str],
    accounts: list[dict[str, Any]],
    mint: str,
) -> str | None:
    signer_set = {item["pubkey"] for item in accounts if item.get("signer")}
    candidates = [pubkey for pubkey in ix_accounts if pubkey in signer_set]
    unique: list[str] = []
    for pubkey in candidates:
        if pubkey not in unique:
            unique.append(pubkey)
    if len(unique) == 1:
        return unique[0]
    not_mint = [pubkey for pubkey in unique if pubkey != mint]
    if len(not_mint) == 1:
        return not_mint[0]
    return None


def _walk_instructions(payload: dict[str, Any], accounts: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    message = _message(payload)
    if not isinstance(message, dict):
        return None
    outer = message.get("instructions")
    if outer is None:
        outer = []
    if not isinstance(outer, list):
        return None
    collected: list[dict[str, Any]] = []
    for ix in outer:
        parsed = _normalize_instruction(ix, accounts)
        if parsed is None:
            return None
        collected.append(parsed)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    inner_groups = meta.get("innerInstructions") if isinstance(meta, dict) else None
    if inner_groups is None:
        return collected
    if not isinstance(inner_groups, list):
        return None
    for group in inner_groups:
        if not isinstance(group, dict):
            return None
        inner = group.get("instructions")
        if inner is None:
            continue
        if not isinstance(inner, list):
            return None
        for ix in inner:
            parsed = _normalize_instruction(ix, accounts)
            if parsed is None:
                return None
            collected.append(parsed)
    return collected


def _normalize_instruction(ix: Any, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(ix, dict):
        return None
    program_id = ix.get("programId") or ix.get("programIdIndex")
    if isinstance(program_id, int):
        if program_id < 0 or program_id >= len(accounts):
            return None
        program_id = accounts[program_id]["pubkey"]
    if not isinstance(program_id, str):
        program_id = None
    parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else {}
    parsed_type = parsed.get("type") if isinstance(parsed, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) and isinstance(parsed.get("info"), dict) else {}
    info_mint = info.get("mint") if isinstance(info.get("mint"), str) else None
    raw_accounts = ix.get("accounts")
    pubkeys: list[str] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if isinstance(item, str) and item.strip():
                pubkeys.append(item.strip())
            elif isinstance(item, int) and 0 <= item < len(accounts):
                pubkeys.append(accounts[item]["pubkey"])
            else:
                return None
    return {
        "program_id": program_id,
        "parsed_type": parsed_type if isinstance(parsed_type, str) else None,
        "info_mint": info_mint,
        "account_pubkeys": pubkeys,
        "data": ix.get("data"),
    }


def _account_entries(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    message = _message(payload)
    if not isinstance(message, dict):
        return None
    keys = message.get("accountKeys")
    if not isinstance(keys, list):
        return None
    entries: list[dict[str, Any]] = []
    header = message.get("header") if isinstance(message.get("header"), dict) else {}
    num_signers = _as_int(header.get("numRequiredSignatures")) or 0
    for index, item in enumerate(keys):
        if isinstance(item, str):
            entries.append({"pubkey": item, "signer": index < num_signers, "writable": True})
        elif isinstance(item, dict) and isinstance(item.get("pubkey"), str):
            entries.append(
                {
                    "pubkey": item["pubkey"],
                    "signer": bool(item.get("signer")),
                    "writable": bool(item.get("writable", True)),
                }
            )
        else:
            return None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    loaded = meta.get("loadedAddresses") if isinstance(meta, dict) else None
    if isinstance(loaded, dict):
        for group_name in ("writable", "readonly"):
            group = loaded.get(group_name)
            if group is None:
                continue
            if not isinstance(group, list):
                return None
            for item in group:
                if not isinstance(item, str):
                    return None
                entries.append(
                    {
                        "pubkey": item,
                        "signer": False,
                        "writable": group_name == "writable",
                    }
                )
    return entries


def _message(payload: dict[str, Any]) -> dict[str, Any] | None:
    tx = payload.get("transaction")
    if not isinstance(tx, dict):
        return None
    message = tx.get("message")
    return message if isinstance(message, dict) else None


def _first_signature(payload: dict[str, Any]) -> str | None:
    tx = payload.get("transaction")
    if not isinstance(tx, dict):
        return None
    signatures = tx.get("signatures")
    if isinstance(signatures, list) and signatures and isinstance(signatures[0], str):
        return signatures[0]
    return None


def _is_signer(pubkey: str, accounts: list[dict[str, Any]]) -> bool:
    for item in accounts:
        if item.get("pubkey") == pubkey:
            return bool(item.get("signer"))
    return False


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58_ALPHABET)}


def _is_pump_create_data(data: Any) -> bool:
    if not isinstance(data, str) or not data.strip():
        return False
    raw = data.strip()
    decoded = _b58decode_any(raw)
    if decoded is None:
        try:
            import base64

            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return False
    return len(decoded) >= 8 and decoded[:8] == PUMP_CREATE_DISCRIMINATOR


def _b58decode_any(raw: str) -> bytes | None:
    number = 0
    for char in raw:
        if char not in _B58_INDEX:
            return None
        number = number * 58 + _B58_INDEX[char]
    payload = number.to_bytes((number.bit_length() + 7) // 8 or 1, "big")
    pad = len(raw) - len(raw.lstrip("1"))
    return (b"\x00" * pad) + payload.lstrip(b"\x00")


def _unsupported_note(instructions: list[dict[str, Any]]) -> str:
    types = [ix.get("parsed_type") or "unparsed" for ix in instructions]
    return (
        "No supported InitializeMint/InitializeMint2 or confidently matched Pump.fun create "
        f"instruction was found. Seen instruction types: {', '.join(types[:12]) or 'none'}."
    )


def _apply_rpc_error(result: MintInitInspection, exc: SolanaRpcError, method: str) -> None:
    message = str(exc)
    result.errors.append(f"{method}: {message}")
    unavailable = (
        exc.is_rate_limited
        or exc.kind in {KIND_RATE_LIMITED, KIND_TIMEOUT}
        or exc.status_code in {408, 429, 500, 502, 503, 504}
        or "Timeout" in message
        or "Rate limited" in message
        or "Retryable HTTP" in message
        or "null" in message
    )
    if unavailable:
        result.inspection_status = STATUS_UNAVAILABLE
        result.mint_init_status = STATUS_UNAVAILABLE
        result.parser_notes = f"RPC could not provide {method} evidence."
        return
    result.inspection_status = STATUS_UNKNOWN
    result.mint_init_status = STATUS_UNKNOWN
    result.parser_notes = f"{method} failed. Missing evidence is UNKNOWN."


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
