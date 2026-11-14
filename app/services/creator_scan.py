"""Bounded mint-initialization creator scan. Manual only; no discovery lock."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import CreatorObservation, IntelligenceScan, Token, utc_now
from app.services.mint_initialization import MintInitInspection, inspect_mint_initialization
from app.services.rpc_accounting import (
    KIND_RATE_LIMITED,
    RpcAccounting,
    accounting_payload,
    mark_batch_stop,
    merge_accounting,
)
from app.services.solana_rpc import SolanaRpcClient

logger = logging.getLogger("meme_hunter.creator")

SCAN_TYPE = "creator"
CREATOR_SCAN_HARD_MAX = 25
CREATOR_SCAN_DEFAULT = 5


@dataclass
class CreatorScanSummary:
    status: str
    tokens_considered: int = 0
    tokens_scanned: int = 0
    verified: int = 0
    unknown: int = 0
    unavailable: int = 0
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int = CREATOR_SCAN_HARD_MAX
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rpc_calls: int = 0
    rpc_successes: int = 0
    rpc_failures: int = 0
    rpc_retries: int = 0
    rpc_http_429: int = 0
    rpc_timeouts: int = 0
    rpc_other_errors: int = 0
    rpc_cache_hits: int = 0
    rpc_accounting: RpcAccounting | None = None
    stop_reason: str | None = None
    stop_token: str | None = None
    note: str = (
        "Mint initialization evidence only. Fee payer is not assumed to be the creator. "
        "Addresses are not identified as people. Paper trading only."
    )


def clamp_creator_limit(limit: int | None, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    requested = CREATOR_SCAN_DEFAULT if limit is None else int(limit)
    configured = min(max(int(settings.creator_scan_limit), 1), CREATOR_SCAN_HARD_MAX)
    return max(1, min(requested, configured, CREATOR_SCAN_HARD_MAX))


async def scan_creator_tokens(
    session: Session,
    rpc: SolanaRpcClient,
    settings: Settings | None = None,
    token_address: str | None = None,
    limit: int | None = None,
) -> CreatorScanSummary:
    settings = settings or get_settings()
    requested = CREATOR_SCAN_DEFAULT if limit is None else int(limit)
    applied = 1 if token_address else clamp_creator_limit(requested, settings)
    summary = CreatorScanSummary(
        status="running",
        started_at=datetime.now(timezone.utc),
        requested_limit=requested,
        applied_limit=applied,
        max_scan_limit=CREATOR_SCAN_HARD_MAX,
    )
    scan_row = IntelligenceScan(scan_type=SCAN_TYPE, started_at=summary.started_at, status="running")
    session.add(scan_row)
    session.commit()

    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    if not token_address:
        tokens = tokens[:applied]
    summary.tokens_considered = len(tokens)

    if rpc.batch_should_stop:
        summary.status = "stopped"
        summary.stop_reason = rpc.accounting.stop_reason or rpc.halt_reason or KIND_RATE_LIMITED
        summary.finished_at = datetime.now(timezone.utc)
        scan_row.finished_at = summary.finished_at
        scan_row.status = summary.status
        scan_row.items_fetched = summary.tokens_considered
        scan_row.items_saved = 0
        scan_row.details_json = json.dumps(_scan_details(summary))
        scan_row.errors_json = json.dumps(summary.errors)
        session.commit()
        return summary

    for token in tokens:
        try:
            inspection = await inspect_mint_initialization(token.token_address, rpc)
            persist_creator_observation(session, token, inspection)
            summary.tokens_scanned += 1
            if inspection.rpc_accounting is not None:
                merge_accounting(summary, inspection.rpc_accounting)
            else:
                summary.rpc_calls += inspection.rpc_calls
            if inspection.inspection_status == "VERIFIED":
                summary.verified += 1
            elif inspection.inspection_status == "UNAVAILABLE":
                summary.unavailable += 1
            else:
                summary.unknown += 1
            if rpc.batch_should_stop:
                mark_batch_stop(summary, token.token_address)
                break
        except Exception as exc:  # noqa: BLE001
            logger.exception("Creator scan failed for %s", token.token_address)
            summary.errors.append({"token_address": token.token_address, "detail": str(exc)})
            session.rollback()
            if rpc.batch_should_stop:
                mark_batch_stop(summary, token.token_address)
                break

    if summary.status == "running":
        summary.status = "completed"
    summary.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = summary.finished_at
    scan_row.status = summary.status
    scan_row.items_fetched = summary.tokens_considered
    scan_row.items_saved = summary.tokens_scanned
    scan_row.details_json = json.dumps(_scan_details(summary))
    scan_row.errors_json = json.dumps(summary.errors)
    session.commit()
    return summary


def persist_creator_observation(
    session: Session,
    token: Token,
    inspection: MintInitInspection,
    recorded_at: datetime | None = None,
) -> CreatorObservation:
    row = CreatorObservation(
        token_id=token.id,
        mint_address=inspection.mint,
        token_program=inspection.token_program,
        inspection_status=inspection.inspection_status,
        mint_init_signature=inspection.mint_init_signature,
        mint_init_slot=inspection.mint_init_slot,
        mint_init_block_time=inspection.mint_init_block_time,
        mint_init_signer=inspection.mint_init_signer,
        mint_init_status=inspection.mint_init_status,
        fee_payer=inspection.fee_payer,
        signer_addresses_json=json.dumps(inspection.signer_addresses),
        create_program_id=inspection.create_program_id,
        create_instruction=inspection.create_instruction,
        source=inspection.source,
        observed_at=inspection.observed_at,
        recorded_at=recorded_at or utc_now(),
        attempted_at=inspection.attempted_at,
        slot=inspection.slot,
        parser_notes=inspection.parser_notes,
        errors_json=json.dumps(inspection.errors),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_creator_scan(session: Session) -> IntelligenceScan | None:
    return session.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == SCAN_TYPE)
        .order_by(IntelligenceScan.id.desc())
        .limit(1)
    )


def latest_creator_observation(session: Session, token_id: int) -> CreatorObservation | None:
    return session.scalar(
        select(CreatorObservation)
        .where(CreatorObservation.token_id == token_id)
        .order_by(CreatorObservation.id.desc())
        .limit(1)
    )


def _scan_details(summary: CreatorScanSummary) -> dict:
    return {
        "verified": summary.verified,
        "unknown": summary.unknown,
        "unavailable": summary.unavailable,
        "requested_limit": summary.requested_limit,
        "applied_limit": summary.applied_limit,
        "max_scan_limit": summary.max_scan_limit,
        "rpc_calls": summary.rpc_calls,
        "rpc_accounting": accounting_payload(summary),
        "stop_reason": summary.stop_reason,
        "stop_token": summary.stop_token,
    }


def creator_observation_history(session: Session, token_id: int) -> list[CreatorObservation]:
    return list(
        session.scalars(
            select(CreatorObservation)
            .where(CreatorObservation.token_id == token_id)
            .order_by(CreatorObservation.id.asc())
        ).all()
    )
