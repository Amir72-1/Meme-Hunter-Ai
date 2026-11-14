"""Token-account owner resolution and conservative classification. Not identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TokenAccountOwnerObservation
from app.services.holder_analysis import BURN_ADDRESSES
from app.services.rpc_accounting import KIND_RATE_LIMITED, KIND_TIMEOUT, RpcAccounting, attach_accounting
from app.services.solana_pda import is_valid_pubkey
from app.services.solana_rpc import RpcAccountResult, SolanaRpcClient, SolanaRpcError
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from app.timeutil import as_utc, utc_now

STATUS_VERIFIED = "VERIFIED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_UNAVAILABLE = "UNAVAILABLE"

CLASS_WALLET = "WALLET"
CLASS_BURN = "BURN_OR_INCINERATOR"
CLASS_AMM = "AMM_OR_LIQUIDITY_VAULT"
CLASS_PROTOCOL = "PROTOCOL_ACCOUNT"
CLASS_UNKNOWN = "UNKNOWN"

SYSTEM_PROGRAM = "11111111111111111111111111111111"
SOURCE = "solana_rpc"
MAX_TOKEN_ACCOUNTS = 20
MAX_OWNER_LOOKUPS = 20

AMM_PROGRAM_IDS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium authority",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca Token Swap",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora AMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "OpenBook",
    "PhoeNiXZ8ByJGLoaCpDMqkSpsN3HUys1pqq5n2oSFvP": "Phoenix",
}

PROTOCOL_PROGRAM_IDS = {
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s": "Metaplex Token Metadata",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token program",
    "Stake11111111111111111111111111111111111111": "Stake program",
    "Vote111111111111111111111111111111111111111": "Vote program",
}


@dataclass
class TokenAccountOwnerRecord:
    mint: str
    token_account_address: str | None = None
    token_account_owner: str | None = None
    token_account_amount: float | None = None
    token_account_status: str | None = None
    owner_classification: str = CLASS_UNKNOWN
    classification_status: str = STATUS_UNKNOWN
    classification_evidence: str | None = None
    token_program: str | None = None
    source: str = SOURCE
    observed_at: datetime | None = None
    attempted_at: datetime | None = None
    slot: int | None = None
    parser_notes: str | None = None
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False


@dataclass
class TokenAccountOwnershipInspection:
    mint: str
    records: list[TokenAccountOwnerRecord] = field(default_factory=list)
    rpc_calls: int = 0
    rpc_successes: int = 0
    rpc_failures: int = 0
    rpc_retries: int = 0
    rpc_http_429: int = 0
    rpc_timeouts: int = 0
    rpc_other_errors: int = 0
    rpc_cache_hits: int = 0
    rpc_accounting: RpcAccounting | None = None
    largest_account_requests: int = 0
    account_info_requests: int = 0
    attempted_at: datetime | None = None
    accounts_considered: int = 0
    accounts_resolved: int = 0
    owner_lookups: int = 0
    parser_notes: str | None = None
    errors: list[str] = field(default_factory=list)


async def inspect_token_account_ownership(
    mint: str,
    rpc: SolanaRpcClient,
    *,
    max_accounts: int = MAX_TOKEN_ACCOUNTS,
) -> TokenAccountOwnershipInspection:
    inspection = TokenAccountOwnershipInspection(mint=mint.strip(), attempted_at=utc_now())
    bound = max(1, min(int(max_accounts), MAX_TOKEN_ACCOUNTS))
    start = rpc.accounting.copy()
    try:
        return await _inspect_token_account_ownership(inspection, rpc, bound)
    finally:
        attach_accounting(inspection, rpc.accounting.delta(start), rpc.last_attempt_at or inspection.attempted_at)
        for record in inspection.records:
            if record.attempted_at is None:
                record.attempted_at = inspection.attempted_at


async def _inspect_token_account_ownership(
    inspection: TokenAccountOwnershipInspection,
    rpc: SolanaRpcClient,
    bound: int,
) -> TokenAccountOwnershipInspection:
    if not is_valid_pubkey(inspection.mint):
        record = TokenAccountOwnerRecord(
            mint=inspection.mint,
            attempted_at=inspection.attempted_at,
            parser_notes="Mint address is not a valid public key.",
            errors=["Invalid mint address"],
        )
        inspection.records.append(record)
        inspection.errors.append("Invalid mint address")
        return inspection

    try:
        largest = await rpc.get_token_largest_accounts_result(inspection.mint)
    except SolanaRpcError as exc:
        record = _rpc_failure_record(inspection.mint, exc, "getTokenLargestAccounts")
        record.attempted_at = inspection.attempted_at
        inspection.records.append(record)
        inspection.errors.extend(record.errors)
        return inspection

    accounts = largest.value[:bound]
    inspection.accounts_considered = len(accounts)
    if largest.value and len(largest.value) > bound:
        inspection.parser_notes = (
            f"Largest-account list was truncated to {bound} lookups. "
            "Top token accounts are not a complete holder census."
        )
    if not accounts:
        inspection.records.append(
            TokenAccountOwnerRecord(
                mint=inspection.mint,
                observed_at=largest.observed_at,
                slot=largest.slot,
                from_cache=largest.from_cache,
                parser_notes=(
                    "No largest token accounts were returned. Missing accounts are UNKNOWN, "
                    "not a complete holder census and not verified absence."
                ),
            )
        )
        return inspection

    owner_cache: dict[str, RpcAccountResult | SolanaRpcError] = {}
    for item in accounts:
        address = str(item.get("address") or "").strip()
        listed_amount = _amount(item)
        if not address:
            inspection.records.append(
                TokenAccountOwnerRecord(
                    mint=inspection.mint,
                    token_account_amount=listed_amount,
                    parser_notes="Largest-account entry had no address.",
                    errors=["Missing token account address"],
                )
            )
            continue
        record = await _inspect_one_account(
            inspection,
            rpc,
            address,
            listed_amount,
            owner_cache,
        )
        inspection.records.append(record)
        if record.token_account_owner:
            inspection.accounts_resolved += 1
    if inspection.parser_notes:
        for record in inspection.records:
            if record.parser_notes:
                record.parser_notes = f"{record.parser_notes} {inspection.parser_notes}"
            else:
                record.parser_notes = inspection.parser_notes
    return inspection


def latest_account_observations_as_of(
    session: Session,
    token_id: int,
    as_of: datetime,
) -> list[TokenAccountOwnerObservation]:
    cutoff = as_utc(as_of)
    rows = list(
        session.scalars(
            select(TokenAccountOwnerObservation)
            .where(TokenAccountOwnerObservation.token_id == token_id)
            .order_by(TokenAccountOwnerObservation.id.desc())
        ).all()
    )
    latest: dict[str, TokenAccountOwnerObservation] = {}
    extra: TokenAccountOwnerObservation | None = None
    for row in rows:
        observed = as_utc(row.observed_at) if row.observed_at is not None else None
        if observed is None or cutoff is None or observed > cutoff:
            continue
        key = row.token_account_address
        if not key:
            if extra is None:
                extra = row
            continue
        if key not in latest:
            latest[key] = row
    selected = list(latest.values())
    selected.sort(key=lambda item: item.id)
    return ([extra] if extra is not None else []) + selected


def latest_account_observations(session: Session, token_id: int) -> list[TokenAccountOwnerObservation]:
    rows = list(
        session.scalars(
            select(TokenAccountOwnerObservation)
            .where(TokenAccountOwnerObservation.token_id == token_id)
            .order_by(TokenAccountOwnerObservation.id.desc())
        ).all()
    )
    latest: dict[str, TokenAccountOwnerObservation] = {}
    extra: TokenAccountOwnerObservation | None = None
    for row in rows:
        key = row.token_account_address
        if not key:
            if extra is None:
                extra = row
            continue
        if key not in latest:
            latest[key] = row
    selected = list(latest.values())
    selected.sort(key=lambda item: item.id)
    return ([extra] if extra is not None else []) + selected


def account_observation_history(session: Session, token_id: int) -> list[TokenAccountOwnerObservation]:
    return list(
        session.scalars(
            select(TokenAccountOwnerObservation)
            .where(TokenAccountOwnerObservation.token_id == token_id)
            .order_by(TokenAccountOwnerObservation.id.asc())
        ).all()
    )


async def _inspect_one_account(
    inspection: TokenAccountOwnershipInspection,
    rpc: SolanaRpcClient,
    address: str,
    listed_amount: float | None,
    owner_cache: dict[str, RpcAccountResult | SolanaRpcError],
) -> TokenAccountOwnerRecord:
    record = TokenAccountOwnerRecord(
        mint=inspection.mint,
        token_account_address=address,
        token_account_amount=listed_amount,
    )
    try:
        account = await rpc.get_account_info(address, encoding="jsonParsed")
    except SolanaRpcError as exc:
        _apply_rpc_error(record, exc, "getAccountInfo")
        record.parser_notes = (
            "Token-account lookup failed. Missing account data is not verified absence "
            "and is not a holder identity."
        )
        return record

    record.from_cache = account.from_cache
    if account.value is None:
        record.classification_status = STATUS_UNAVAILABLE
        record.owner_classification = CLASS_UNKNOWN
        record.parser_notes = (
            "RPC returned no token-account body. This is UNAVAILABLE evidence, "
            "not proof that the account or owner is absent."
        )
        record.errors.append("getAccountInfo returned null")
        return record

    parsed = _parse_token_account(inspection.mint, address, account, listed_amount)
    record.__dict__.update({key: getattr(parsed, key) for key in parsed.__dict__})
    if parsed.classification_status == STATUS_UNAVAILABLE or not parsed.token_account_owner:
        return record
    owner = parsed.token_account_owner
    if owner in BURN_ADDRESSES:
        record.owner_classification = CLASS_BURN
        record.classification_status = STATUS_VERIFIED
        record.classification_evidence = (
            "Token-account owner pubkey matches a known burn or incinerator address. "
            "This is not a person and is not a creator claim."
        )
        return record
    owner_account = await _owner_account(inspection, rpc, owner, owner_cache)
    if isinstance(owner_account, SolanaRpcError):
        _apply_rpc_error(record, owner_account, "getAccountInfo(owner)")
        record.owner_classification = CLASS_UNKNOWN
        record.parser_notes = (
            (record.parser_notes + " ") if record.parser_notes else ""
        ) + "Owner-account lookup failed. Token-account owner is not classified as a wallet or vault."
        return record
    if owner_account.value is None:
        record.owner_classification = CLASS_UNKNOWN
        record.classification_status = STATUS_UNAVAILABLE
        record.classification_evidence = (
            "Owner account body was not returned. Missing owner data is not verified absence."
        )
        return record
    program_id = owner_account.value.get("owner")
    if not isinstance(program_id, str) or not program_id:
        record.owner_classification = CLASS_UNKNOWN
        record.classification_status = STATUS_UNKNOWN
        record.classification_evidence = "Owner account had no program owner field."
        record.parser_notes = "Unsupported owner-account format."
        return record
    if program_id in AMM_PROGRAM_IDS:
        record.owner_classification = CLASS_AMM
        record.classification_status = STATUS_VERIFIED
        record.classification_evidence = (
            f"Owner account is owned by known AMM/liquidity program {program_id} "
            f"({AMM_PROGRAM_IDS[program_id]}). Token account address is not a pool address. "
            "Large balances are not used as vault evidence."
        )
        return record
    if program_id in PROTOCOL_PROGRAM_IDS:
        record.owner_classification = CLASS_PROTOCOL
        record.classification_status = STATUS_VERIFIED
        record.classification_evidence = (
            f"Owner account is owned by known protocol program {program_id} "
            f"({PROTOCOL_PROGRAM_IDS[program_id]}). This is not a person."
        )
        return record
    if program_id == SYSTEM_PROGRAM:
        record.owner_classification = CLASS_WALLET
        record.classification_status = STATUS_VERIFIED
        record.classification_evidence = (
            "Owner account is owned by the System Program and is not a known burn address. "
            "This is a wallet-shaped account, not a person or token creator."
        )
        return record
    record.owner_classification = CLASS_UNKNOWN
    record.classification_status = STATUS_UNKNOWN
    record.classification_evidence = (
        f"Owner account program {program_id} is not in the supported AMM or protocol list. "
        "Classification remains UNKNOWN. A large token balance is not AMM evidence."
    )
    return record


def _parse_token_account(
    mint: str,
    address: str,
    account: RpcAccountResult,
    listed_amount: float | None,
) -> TokenAccountOwnerRecord:
    record = TokenAccountOwnerRecord(
        mint=mint,
        token_account_address=address,
        token_account_amount=listed_amount,
        observed_at=account.observed_at,
        slot=account.slot,
        from_cache=account.from_cache,
    )
    value = account.value or {}
    program_id = value.get("owner")
    record.token_program = program_id if isinstance(program_id, str) else None
    if record.token_program not in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
        record.classification_status = STATUS_UNKNOWN
        record.parser_notes = (
            "Account is not owned by SPL Token or Token-2022. Unsupported account data remains UNKNOWN."
        )
        return record
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict):
        record.classification_status = STATUS_UNKNOWN
        record.parser_notes = "jsonParsed token-account payload was missing. Unsupported format remains UNKNOWN."
        return record
    if parsed.get("type") != "account":
        record.classification_status = STATUS_UNKNOWN
        record.parser_notes = f"Parsed account type {parsed.get('type')!r} is not a token account."
        return record
    info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
    info_mint = info.get("mint")
    if isinstance(info_mint, str) and info_mint and info_mint != mint:
        record.classification_status = STATUS_UNKNOWN
        record.parser_notes = "Parsed mint does not match the requested mint. Observation is not attached as verified."
        record.errors.append("Mint identity mismatch")
        return record
    owner = info.get("owner")
    record.token_account_owner = owner.strip() if isinstance(owner, str) and owner.strip() else None
    state = info.get("state")
    record.token_account_status = str(state) if state is not None else STATUS_UNKNOWN
    parsed_amount = _token_amount(info)
    if parsed_amount is not None:
        record.token_account_amount = parsed_amount
    if record.token_account_owner is None:
        record.classification_status = STATUS_UNKNOWN
        record.parser_notes = "Token account had no owner field. Owner remains UNKNOWN."
    else:
        record.parser_notes = (
            "Token-account owner was parsed from jsonParsed data. "
            "The token-account address is not a wallet address by itself."
        )
    return record


async def _owner_account(
    inspection: TokenAccountOwnershipInspection,
    rpc: SolanaRpcClient,
    owner: str,
    cache: dict[str, RpcAccountResult | SolanaRpcError],
) -> RpcAccountResult | SolanaRpcError:
    cached = cache.get(owner)
    if cached is not None:
        return cached
    if inspection.owner_lookups >= MAX_OWNER_LOOKUPS:
        error = SolanaRpcError("Owner lookup budget exhausted")
        cache[owner] = error
        return error
    try:
        result = await rpc.get_account_info(owner, encoding="jsonParsed")
        inspection.owner_lookups += 1
        cache[owner] = result
        return result
    except SolanaRpcError as exc:
        inspection.owner_lookups += 1
        cache[owner] = exc
        return exc


def _rpc_failure_record(mint: str, exc: SolanaRpcError, method: str) -> TokenAccountOwnerRecord:
    record = TokenAccountOwnerRecord(mint=mint, errors=[f"{method}: {exc}"])
    _apply_rpc_error(record, exc, method)
    record.parser_notes = f"RPC could not provide {method} evidence for this mint."
    return record


def _apply_rpc_error(record: TokenAccountOwnerRecord, exc: SolanaRpcError, method: str) -> None:
    message = str(exc)
    record.errors.append(f"{method}: {message}")
    unavailable = (
        exc.is_rate_limited
        or exc.kind in {KIND_RATE_LIMITED, KIND_TIMEOUT}
        or exc.status_code in {408, 429, 500, 502, 503, 504}
        or "Timeout" in message
        or "Rate limited" in message
        or "Retryable HTTP" in message
        or "budget exhausted" in message
    )
    if unavailable:
        record.classification_status = STATUS_UNAVAILABLE
        record.owner_classification = CLASS_UNKNOWN
        return
    record.classification_status = STATUS_UNKNOWN
    record.owner_classification = CLASS_UNKNOWN


def _token_amount(info: dict[str, Any]) -> float | None:
    token_amount = info.get("tokenAmount")
    if isinstance(token_amount, dict):
        parsed = _amount(token_amount)
        if parsed is not None:
            return parsed
    return _amount(info)


def _amount(payload: dict[str, Any]) -> float | None:
    ui = payload.get("uiAmount")
    if ui is not None:
        try:
            return float(ui)
        except (TypeError, ValueError):
            return None
    amount = payload.get("amount")
    if amount is None or amount == "":
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None
