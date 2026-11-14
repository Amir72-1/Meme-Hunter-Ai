"""Bounded token-account owner scan. Manual only; no discovery lock."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import IntelligenceScan, Token, TokenAccountOwnerObservation, utc_now
from app.services.creator_scan import CREATOR_SCAN_DEFAULT, CREATOR_SCAN_HARD_MAX, clamp_creator_limit
from app.services.rpc_accounting import (
    KIND_RATE_LIMITED,
    RpcAccounting,
    accounting_payload,
    mark_batch_stop,
    merge_accounting,
)
from app.services.solana_rpc import SolanaRpcClient
from app.services.token_account_ownership import (
    TokenAccountOwnerRecord,
    inspect_token_account_ownership,
)

logger = logging.getLogger("meme_hunter.holder_owners")

SCAN_TYPE = "creator_holders"


@dataclass
class HolderOwnerScanSummary:
    status: str
    tokens_considered: int = 0
    tokens_scanned: int = 0
    accounts_observed: int = 0
    verified: int = 0
    unknown: int = 0
    unavailable: int = 0
    requested_limit: int | None = None
    applied_limit: int | None = None
    max_scan_limit: int = CREATOR_SCAN_HARD_MAX
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
    unique_owner_lookups: int = 0
    stop_reason: str | None = None
    stop_token: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str = (
        "Top token accounts are not a complete holder census. "
        "Token-account addresses are not wallets. Owners are not identified as people. "
        "Paper trading only."
    )


async def scan_holder_owners(
    session: Session,
    rpc: SolanaRpcClient,
    settings: Settings | None = None,
    token_address: str | None = None,
    limit: int | None = None,
) -> HolderOwnerScanSummary:
    settings = settings or get_settings()
    requested = CREATOR_SCAN_DEFAULT if limit is None else int(limit)
    applied = 1 if token_address else clamp_creator_limit(requested, settings)
    summary = HolderOwnerScanSummary(
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
            inspection = await inspect_token_account_ownership(token.token_address, rpc)
            persist_owner_observations(session, token, inspection.records)
            summary.tokens_scanned += 1
            if inspection.rpc_accounting is not None:
                merge_accounting(summary, inspection.rpc_accounting)
            else:
                summary.rpc_calls += inspection.rpc_calls
            summary.largest_account_requests += inspection.largest_account_requests
            summary.account_info_requests += inspection.account_info_requests
            summary.unique_owner_lookups += inspection.owner_lookups
            summary.accounts_observed += len(inspection.records)
            for record in inspection.records:
                if record.classification_status == "VERIFIED":
                    summary.verified += 1
                elif record.classification_status == "UNAVAILABLE":
                    summary.unavailable += 1
                else:
                    summary.unknown += 1
            if rpc.batch_should_stop:
                mark_batch_stop(summary, token.token_address)
                break
        except Exception as exc:  # noqa: BLE001
            logger.exception("Holder-owner scan failed for %s", token.token_address)
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


def persist_owner_observations(
    session: Session,
    token: Token,
    records: list[TokenAccountOwnerRecord],
    recorded_at: datetime | None = None,
) -> list[TokenAccountOwnerObservation]:
    inserted: list[TokenAccountOwnerObservation] = []
    stamp = recorded_at or utc_now()
    for record in records:
        row = TokenAccountOwnerObservation(
            token_id=token.id,
            mint_address=record.mint,
            token_account_address=record.token_account_address,
            token_account_owner=record.token_account_owner,
            token_account_amount=record.token_account_amount,
            token_account_status=record.token_account_status,
            owner_classification=record.owner_classification,
            classification_status=record.classification_status,
            classification_evidence=record.classification_evidence,
            token_program=record.token_program,
            source=record.source,
            observed_at=record.observed_at,
            recorded_at=stamp,
            attempted_at=record.attempted_at,
            slot=record.slot,
            parser_notes=record.parser_notes,
            errors_json=json.dumps(record.errors),
        )
        session.add(row)
        inserted.append(row)
    session.commit()
    for row in inserted:
        session.refresh(row)
    return inserted


def _scan_details(summary: HolderOwnerScanSummary) -> dict:
    payload = {
        "verified": summary.verified,
        "unknown": summary.unknown,
        "unavailable": summary.unavailable,
        "accounts_observed": summary.accounts_observed,
        "requested_limit": summary.requested_limit,
        "applied_limit": summary.applied_limit,
        "max_scan_limit": summary.max_scan_limit,
        "rpc_calls": summary.rpc_calls,
        "rpc_accounting": accounting_payload(summary),
        "largest_account_requests": summary.largest_account_requests,
        "account_info_requests": summary.account_info_requests,
        "unique_owner_lookups": summary.unique_owner_lookups,
        "stop_reason": summary.stop_reason,
        "stop_token": summary.stop_token,
    }
    return payload


def latest_holder_owner_scan(session: Session) -> IntelligenceScan | None:
    return session.scalar(
        select(IntelligenceScan)
        .where(IntelligenceScan.scan_type == SCAN_TYPE)
        .order_by(IntelligenceScan.id.desc())
        .limit(1)
    )
