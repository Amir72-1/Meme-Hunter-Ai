"""Mint-initialization creator evidence API. Manual scan only."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import CreatorObservation, MetaplexCreatorObservation, Token, TokenAccountOwnerObservation
from app.schemas import (
    AddressMatchOut,
    CreatorEvidencePanelResponse,
    CreatorHistoryResponse,
    CreatorObservationOut,
    CreatorScanRequest,
    CreatorScanResponse,
    CreatorStatusResponse,
    CreatorTokenResponse,
    EvidenceBatchRequest,
    EvidenceBatchResponse,
    EvidenceOperationOut,
    HolderEvidenceSection,
    HolderOwnerHistoryResponse,
    HolderOwnerScanRequest,
    HolderOwnerScanResponse,
    HolderOwnerStatusResponse,
    HolderOwnerTokenResponse,
    MetaplexCreatorEntryOut,
    MetaplexCreatorHistoryResponse,
    MetaplexCreatorObservationOut,
    MetaplexCreatorScanRequest,
    MetaplexCreatorScanResponse,
    MetaplexCreatorStatusResponse,
    MetaplexCreatorTokenResponse,
    MetaplexEvidenceSection,
    MintInitEvidenceSection,
    RpcCooldownResponse,
    TokenAccountOwnerOut,
)
from app.services.creator_evidence import build_creator_evidence_panel
from app.services.creator_scan import (
    creator_observation_history,
    latest_creator_observation,
    latest_creator_scan,
    scan_creator_tokens,
)
from app.services.holder_owner_scan import latest_holder_owner_scan, scan_holder_owners
from app.services.metaplex_creator_scan import latest_metaplex_creator_scan, scan_metaplex_creators
from app.services.metaplex_creators import (
    latest_metaplex_creator_observation,
    latest_observation_as_of as latest_metaplex_creator_as_of,
    metaplex_creator_history,
)
from app.services.mint_initialization import latest_observation_as_of
from app.services.creator_evidence_batch import run_creator_evidence_batch
from app.services.rpc_accounting import KIND_COOLDOWN_ACTIVE, accounting_payload
from app.services.rpc_cooldown import cooldown_payload
from app.services.rpc_session import EvidenceGate, evidence_rpc_gate, get_shared_rpc_session
from app.services.token_account_ownership import (
    account_observation_history,
    latest_account_observations,
    latest_account_observations_as_of,
)

router = APIRouter(prefix="/api/v1/creator", tags=["creator"])


def _raise_for_gate(gate: EvidenceGate) -> None:
    if gate.kind == "ok" and gate.rpc is not None:
        return
    if gate.kind == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "status": "BATCH_IN_PROGRESS",
                "detail": "A creator evidence scan is already running. Conflicting scans are rejected.",
            },
        )
    if gate.kind == "shutdown":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shared Solana RPC session is shut down",
        )
    cooldown = gate.cooldown or cooldown_payload()
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "status": KIND_COOLDOWN_ACTIVE,
            "stop_reason": KIND_COOLDOWN_ACTIVE,
            "rpc_calls": 0,
            "detail": "Solana RPC cooldown is active. No RPC request was sent.",
            **cooldown,
        },
    )


@router.post("/scan", response_model=CreatorScanResponse)
async def scan_creator(
    payload: CreatorScanRequest = Body(default_factory=CreatorScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CreatorScanResponse:
    async with evidence_rpc_gate(settings) as gate:
        _raise_for_gate(gate)
        result = await scan_creator_tokens(
            db,
            gate.rpc,
            settings,
            token_address=payload.token_address,
            limit=payload.limit,
        )
    return CreatorScanResponse(
        status=result.status,
        tokens_considered=result.tokens_considered,
        tokens_scanned=result.tokens_scanned,
        verified=result.verified,
        unknown=result.unknown,
        unavailable=result.unavailable,
        requested_limit=result.requested_limit,
        applied_limit=result.applied_limit,
        max_scan_limit=result.max_scan_limit,
        rpc_calls=result.rpc_calls,
        rpc_accounting=accounting_payload(result),
        stop_reason=result.stop_reason,
        stop_token=result.stop_token,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
        note=result.note,
    )


@router.get("/rpc/cooldown", response_model=RpcCooldownResponse)
def get_rpc_cooldown() -> RpcCooldownResponse:
    manager = get_shared_rpc_session()
    payload = cooldown_payload()
    return RpcCooldownResponse(
        **payload,
        busy=manager.busy,
        session_closed=manager.closed,
        note=(
            "Shared cooldown for creator evidence RPC. "
            "RATE_LIMITED activates until_at from Retry-After or bounded exponential backoff. "
            "No RPC is sent while active. An expired row with cleared_at set is history, not a block. "
            "Paper trading only."
        ),
    )


@router.post("/batch", response_model=EvidenceBatchResponse)
async def scan_creator_evidence_batch(
    payload: EvidenceBatchRequest = Body(default_factory=EvidenceBatchRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EvidenceBatchResponse:
    async with evidence_rpc_gate(settings) as gate:
        _raise_for_gate(gate)
        result = await run_creator_evidence_batch(
            db,
            gate.rpc,
            settings,
            token_address=payload.token_address,
            limit=payload.limit,
        )
    return EvidenceBatchResponse(
        status=result.status,
        stop_reason=result.stop_reason,
        stop_token=result.stop_token,
        operations=[
            EvidenceOperationOut(
                name=item.name,
                token_address=item.token_address,
                status=item.status,
                stop_reason=item.stop_reason,
                rpc_calls=item.rpc_calls,
                rpc_accounting=item.rpc_accounting,
            )
            for item in result.operations
        ],
        tokens_planned=result.tokens_planned,
        tokens_started=result.tokens_started,
        tokens_skipped=result.tokens_skipped,
        rpc_calls=result.rpc_calls,
        rpc_accounting=accounting_payload(result),
        cooldown=result.cooldown,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
        note=result.note,
    )


@router.get("/evidence/{mint}", response_model=CreatorEvidencePanelResponse)
def get_creator_evidence(
    mint: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> CreatorEvidencePanelResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    panel = build_creator_evidence_panel(db, token, as_of)
    mint_row = panel.mint_initialization
    holders = panel.token_accounts
    meta_row = panel.metaplex
    missing = (
        "No stored observation for this source at the requested time. "
        "Missing evidence is UNKNOWN, not filled from later data."
    )
    mint_section = MintInitEvidenceSection(
        available=mint_row is not None,
        status=mint_row.inspection_status if mint_row is not None else "UNKNOWN",
        observed_at=mint_row.observed_at if mint_row is not None else None,
        slot=mint_row.slot if mint_row is not None else None,
        observation=_observation_out(panel.mint, mint_row),
        note=(
            "Mint-initialization evidence is independent of Metaplex creators and token-account owners. "
            "The fee payer is not assumed to be the creator."
            if mint_row is not None
            else missing
        ),
    )
    holder_status = "UNKNOWN"
    holder_observed = None
    holder_slot = None
    if holders:
        statuses = {row.classification_status for row in holders}
        if statuses == {"VERIFIED"}:
            holder_status = "VERIFIED"
        elif statuses == {"UNAVAILABLE"}:
            holder_status = "UNAVAILABLE"
        elif statuses == {"UNKNOWN"}:
            holder_status = "UNKNOWN"
        else:
            holder_status = "MIXED"
        timed = [row.observed_at for row in holders if row.observed_at is not None]
        holder_observed = max(timed) if timed else None
        slots = [row.slot for row in holders if row.slot is not None]
        holder_slot = max(slots) if slots else None
    holder_section = HolderEvidenceSection(
        available=bool(holders),
        status=holder_status,
        observed_at=holder_observed,
        slot=holder_slot,
        observations=[_owner_out(row) for row in holders],
        note=(
            "Token-account ownership evidence is independent of mint initialization and Metaplex creators. "
            "Top accounts are not a complete census."
            if holders
            else missing
        ),
    )
    meta_section = MetaplexEvidenceSection(
        available=meta_row is not None,
        status=meta_row.creator_vector_status if meta_row is not None else "UNKNOWN",
        observed_at=meta_row.observed_at if meta_row is not None else None,
        slot=meta_row.slot if meta_row is not None else None,
        observation=_metaplex_out(meta_row),
        note=(
            "Metaplex creators[] evidence is independent of mint-init signers and token-account owners. "
            "verified=true is not identity."
            if meta_row is not None
            else missing
        ),
    )
    return CreatorEvidencePanelResponse(
        token_address=panel.mint,
        symbol=panel.symbol,
        name=panel.name,
        as_of=panel.as_of,
        mint_initialization=mint_section,
        token_account_ownership=holder_section,
        metaplex_creators=meta_section,
        address_matches=[
            AddressMatchOut(
                address=item.address,
                kind=item.kind,
                sources=item.sources,
                roles=item.roles,
                note=item.note,
            )
            for item in panel.address_matches
        ],
        limitations=panel.limitations,
        note=panel.note,
        read_only=True,
        rpc_calls=0,
    )


@router.get("/status", response_model=CreatorStatusResponse)
def creator_status(db: Session = Depends(get_db)) -> CreatorStatusResponse:
    row = latest_creator_scan(db)
    details = json.loads(row.details_json or "{}") if row is not None else {}
    stamp = None
    if row is not None and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return CreatorStatusResponse(
        last_status=row.status if row else "never",
        last_scan_at=stamp,
        last_tokens_scanned=row.items_saved if row else None,
        verified=details.get("verified"),
        unknown=details.get("unknown"),
        unavailable=details.get("unavailable"),
        rpc_calls=details.get("rpc_calls"),
        rpc_accounting=details.get("rpc_accounting"),
        stop_reason=details.get("stop_reason"),
        stop_token=details.get("stop_token"),
        note=(
            "Manual mint-initialization inspection using public Solana RPC. "
            "No automatic recurring creator scan. Fee payer is not assumed to be the creator. "
            "Paper trading only."
        ),
    )


@router.get("/tokens/{mint}", response_model=CreatorTokenResponse)
def get_creator_token(
    mint: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> CreatorTokenResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if as_of is not None:
        observation = latest_observation_as_of(db, token.id, as_of)
        note = (
            "Point-in-time creator observation with observed_at <= as_of. "
            "Later initialization evidence does not fill an earlier signal time. "
            "Fee payer is not assumed to be the creator."
        )
    else:
        observation = latest_creator_observation(db, token.id)
        note = (
            "Latest stored mint-initialization observation. "
            "This is not a person, deployer, or safety label."
        )
    return CreatorTokenResponse(
        token_address=token.token_address,
        symbol=token.symbol,
        name=token.name,
        as_of=as_of,
        observation=_observation_out(token.token_address, observation),
        note=note,
    )


@router.get("/tokens/{mint}/history", response_model=CreatorHistoryResponse)
def get_creator_history(mint: str, db: Session = Depends(get_db)) -> CreatorHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    rows = creator_observation_history(db, token.id)
    return CreatorHistoryResponse(
        token_address=token.token_address,
        observations=[_observation_out(token.token_address, row) for row in rows],
        note=(
            "Append-only mint-initialization observations. Historical rows are never rewritten. "
            "wallet_activities.deployer_address is not modified."
        ),
    )


@router.post("/holders/scan", response_model=HolderOwnerScanResponse)
async def scan_holder_owners_endpoint(
    payload: HolderOwnerScanRequest = Body(default_factory=HolderOwnerScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HolderOwnerScanResponse:
    async with evidence_rpc_gate(settings) as gate:
        _raise_for_gate(gate)
        result = await scan_holder_owners(
            db,
            gate.rpc,
            settings,
            token_address=payload.token_address,
            limit=payload.limit,
        )
    return HolderOwnerScanResponse(
        status=result.status,
        tokens_considered=result.tokens_considered,
        tokens_scanned=result.tokens_scanned,
        accounts_observed=result.accounts_observed,
        verified=result.verified,
        unknown=result.unknown,
        unavailable=result.unavailable,
        requested_limit=result.requested_limit,
        applied_limit=result.applied_limit,
        max_scan_limit=result.max_scan_limit,
        rpc_calls=result.rpc_calls,
        rpc_accounting=accounting_payload(result),
        largest_account_requests=result.largest_account_requests,
        account_info_requests=result.account_info_requests,
        unique_owner_lookups=result.unique_owner_lookups,
        stop_reason=result.stop_reason,
        stop_token=result.stop_token,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
        note=result.note,
    )


@router.get("/holders/status", response_model=HolderOwnerStatusResponse)
def holder_owner_status(db: Session = Depends(get_db)) -> HolderOwnerStatusResponse:
    row = latest_holder_owner_scan(db)
    details = json.loads(row.details_json or "{}") if row is not None else {}
    stamp = None
    if row is not None and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return HolderOwnerStatusResponse(
        last_status=row.status if row else "never",
        last_scan_at=stamp,
        last_tokens_scanned=row.items_saved if row else None,
        accounts_observed=details.get("accounts_observed"),
        verified=details.get("verified"),
        unknown=details.get("unknown"),
        unavailable=details.get("unavailable"),
        rpc_calls=details.get("rpc_calls"),
        rpc_accounting=details.get("rpc_accounting"),
        stop_reason=details.get("stop_reason"),
        stop_token=details.get("stop_token"),
        note=(
            "Manual top-token-account owner inspection using public Solana RPC. "
            "Not a complete holder census. Token accounts are not wallets. "
            "No automatic scan. Paper trading only."
        ),
    )


@router.get("/holders/{mint}", response_model=HolderOwnerTokenResponse)
def get_holder_owners(
    mint: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HolderOwnerTokenResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if as_of is not None:
        rows = latest_account_observations_as_of(db, token.id, as_of)
        note = (
            "Point-in-time token-account owner observations with observed_at <= as_of. "
            "Later ownership is not applied to an earlier signal time. "
            "Top accounts are not a complete census."
        )
    else:
        rows = latest_account_observations(db, token.id)
        note = (
            "Latest stored token-account owner observations. "
            "Owners are not identified as people. Token-account addresses are not wallets."
        )
    return HolderOwnerTokenResponse(
        token_address=token.token_address,
        symbol=token.symbol,
        name=token.name,
        as_of=as_of,
        observations=[_owner_out(row) for row in rows],
        note=note,
    )


@router.get("/holders/{mint}/history", response_model=HolderOwnerHistoryResponse)
def get_holder_owner_history(mint: str, db: Session = Depends(get_db)) -> HolderOwnerHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    rows = account_observation_history(db, token.id)
    return HolderOwnerHistoryResponse(
        token_address=token.token_address,
        observations=[_owner_out(row) for row in rows],
        note=(
            "Append-only token-account owner observations. Historical rows are never rewritten. "
            "Opportunity scoring is unchanged."
        ),
    )


@router.post("/metadata/scan", response_model=MetaplexCreatorScanResponse)
async def scan_metaplex_creators_endpoint(
    payload: MetaplexCreatorScanRequest = Body(default_factory=MetaplexCreatorScanRequest),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MetaplexCreatorScanResponse:
    async with evidence_rpc_gate(settings) as gate:
        _raise_for_gate(gate)
        result = await scan_metaplex_creators(
            db,
            gate.rpc,
            settings,
            token_address=payload.token_address,
            limit=payload.limit,
        )
    return MetaplexCreatorScanResponse(
        status=result.status,
        tokens_considered=result.tokens_considered,
        tokens_scanned=result.tokens_scanned,
        verified=result.verified,
        verified_absent=result.verified_absent,
        unknown=result.unknown,
        unavailable=result.unavailable,
        requested_limit=result.requested_limit,
        applied_limit=result.applied_limit,
        max_scan_limit=result.max_scan_limit,
        rpc_calls=result.rpc_calls,
        rpc_accounting=accounting_payload(result),
        stop_reason=result.stop_reason,
        stop_token=result.stop_token,
        errors=result.errors,
        started_at=result.started_at,
        finished_at=result.finished_at,
        note=result.note,
    )


@router.get("/metadata/status", response_model=MetaplexCreatorStatusResponse)
def metaplex_creator_status(db: Session = Depends(get_db)) -> MetaplexCreatorStatusResponse:
    row = latest_metaplex_creator_scan(db)
    details = json.loads(row.details_json or "{}") if row is not None else {}
    stamp = None
    if row is not None and (row.finished_at or row.started_at):
        stamp = (row.finished_at or row.started_at).isoformat()
    return MetaplexCreatorStatusResponse(
        last_status=row.status if row else "never",
        last_scan_at=stamp,
        last_tokens_scanned=row.items_saved if row else None,
        verified=details.get("verified"),
        verified_absent=details.get("verified_absent"),
        unknown=details.get("unknown"),
        unavailable=details.get("unavailable"),
        rpc_calls=details.get("rpc_calls"),
        rpc_accounting=details.get("rpc_accounting"),
        stop_reason=details.get("stop_reason"),
        stop_token=details.get("stop_token"),
        note=(
            "Manual Metaplex creators[] inspection using public Solana RPC. "
            "verified=true is not identity. No automatic scan. Paper trading only."
        ),
    )


@router.get("/metadata/{mint}", response_model=MetaplexCreatorTokenResponse)
def get_metaplex_creators(
    mint: str,
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> MetaplexCreatorTokenResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if as_of is not None:
        observation = latest_metaplex_creator_as_of(db, token.id, as_of)
        note = (
            "Point-in-time Metaplex creator observation with observed_at <= as_of. "
            "Later metadata does not fill an earlier signal time."
        )
    else:
        observation = latest_metaplex_creator_observation(db, token.id)
        note = (
            "Latest stored Metaplex creators[] observation. "
            "This is not identity, deployer, or a safety label."
        )
    return MetaplexCreatorTokenResponse(
        token_address=token.token_address,
        symbol=token.symbol,
        name=token.name,
        as_of=as_of,
        observation=_metaplex_out(observation),
        note=note,
    )


@router.get("/metadata/{mint}/history", response_model=MetaplexCreatorHistoryResponse)
def get_metaplex_creator_history(mint: str, db: Session = Depends(get_db)) -> MetaplexCreatorHistoryResponse:
    token = db.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    rows = metaplex_creator_history(db, token.id)
    return MetaplexCreatorHistoryResponse(
        token_address=token.token_address,
        observations=[_metaplex_out(row) for row in rows if row is not None],
        note=(
            "Append-only Metaplex creator observations. Historical rows are never rewritten. "
            "Opportunity scoring is unchanged."
        ),
    )


def _metaplex_out(row: MetaplexCreatorObservation | None) -> MetaplexCreatorObservationOut | None:
    if row is None:
        return None
    try:
        errors = json.loads(row.errors_json or "[]")
    except json.JSONDecodeError:
        errors = []
    if not isinstance(errors, list):
        errors = []
    creators = None
    if row.creators_json is not None:
        try:
            parsed = json.loads(row.creators_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            creators = [
                MetaplexCreatorEntryOut(
                    address=item.get("address") if isinstance(item, dict) else None,
                    verified=item.get("verified") if isinstance(item, dict) else None,
                    share=item.get("share") if isinstance(item, dict) else None,
                    order=item.get("order") if isinstance(item, dict) else None,
                )
                for item in parsed
            ]
    unknown_fields = [
        name
        for name, value in (
            ("metadata_account", row.metadata_account),
            ("creators_json", row.creators_json),
            ("creator_count", row.creator_count),
            ("verified_creator_count", row.verified_creator_count),
            ("observed_at", row.observed_at),
            ("slot", row.slot),
        )
        if value is None
    ]
    if row.creator_vector_status not in {"VERIFIED", "VERIFIED_ABSENT"}:
        unknown_fields.append("creator_vector_status")
    return MetaplexCreatorObservationOut(
        mint_address=row.mint_address,
        metadata_account=row.metadata_account,
        metadata_status=row.metadata_status,
        creator_vector_status=row.creator_vector_status,
        creators=creators,
        creator_count=row.creator_count,
        verified_creator_count=row.verified_creator_count,
        source=row.source,
        observed_at=row.observed_at,
        recorded_at=row.recorded_at,
        attempted_at=row.attempted_at,
        slot=row.slot,
        parser_notes=row.parser_notes,
        errors=[str(item) for item in errors],
        unknown_fields=unknown_fields,
    )


def _owner_out(row: TokenAccountOwnerObservation) -> TokenAccountOwnerOut:
    try:
        errors = json.loads(row.errors_json or "[]")
    except json.JSONDecodeError:
        errors = []
    if not isinstance(errors, list):
        errors = []
    unknown_fields = [
        name
        for name, value in (
            ("token_account_address", row.token_account_address),
            ("token_account_owner", row.token_account_owner),
            ("token_account_amount", row.token_account_amount),
            ("token_account_status", row.token_account_status),
            ("classification_evidence", row.classification_evidence),
            ("token_program", row.token_program),
            ("observed_at", row.observed_at),
            ("slot", row.slot),
        )
        if value is None
    ]
    if row.owner_classification == "UNKNOWN":
        unknown_fields.append("owner_classification")
    if row.classification_status != "VERIFIED":
        unknown_fields.append("classification_status")
    return TokenAccountOwnerOut(
        mint_address=row.mint_address,
        token_account_address=row.token_account_address,
        token_account_owner=row.token_account_owner,
        token_account_amount=row.token_account_amount,
        token_account_status=row.token_account_status,
        owner_classification=row.owner_classification,
        classification_status=row.classification_status,
        classification_evidence=row.classification_evidence,
        token_program=row.token_program,
        source=row.source,
        observed_at=row.observed_at,
        recorded_at=row.recorded_at,
        attempted_at=row.attempted_at,
        slot=row.slot,
        parser_notes=row.parser_notes,
        errors=[str(item) for item in errors],
        unknown_fields=unknown_fields,
    )


def _observation_out(token_address: str, row: CreatorObservation | None) -> CreatorObservationOut | None:
    if row is None:
        return None
    try:
        signers = json.loads(row.signer_addresses_json or "[]")
    except json.JSONDecodeError:
        signers = []
    if not isinstance(signers, list):
        signers = []
    try:
        errors = json.loads(row.errors_json or "[]")
    except json.JSONDecodeError:
        errors = []
    if not isinstance(errors, list):
        errors = []
    unknown_fields = [
        name
        for name, value in (
            ("token_program", row.token_program),
            ("mint_init_signature", row.mint_init_signature),
            ("mint_init_slot", row.mint_init_slot),
            ("mint_init_block_time", row.mint_init_block_time),
            ("mint_init_signer", row.mint_init_signer),
            ("fee_payer", row.fee_payer),
            ("create_program_id", row.create_program_id),
            ("create_instruction", row.create_instruction),
            ("observed_at", row.observed_at),
            ("slot", row.slot),
        )
        if value is None
    ]
    if row.mint_init_status != "VERIFIED":
        unknown_fields.append("mint_init_status")
    return CreatorObservationOut(
        token_address=token_address,
        mint_address=row.mint_address,
        token_program=row.token_program,
        inspection_status=row.inspection_status,
        mint_init_signature=row.mint_init_signature,
        mint_init_slot=row.mint_init_slot,
        mint_init_block_time=row.mint_init_block_time,
        mint_init_signer=row.mint_init_signer,
        mint_init_status=row.mint_init_status,
        fee_payer=row.fee_payer,
        signer_addresses=[item for item in signers if isinstance(item, str)],
        create_program_id=row.create_program_id,
        create_instruction=row.create_instruction,
        source=row.source,
        observed_at=row.observed_at,
        recorded_at=row.recorded_at,
        attempted_at=row.attempted_at,
        slot=row.slot,
        parser_notes=row.parser_notes,
        errors=[str(item) for item in errors],
        unknown_fields=unknown_fields,
    )
