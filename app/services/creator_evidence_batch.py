"""One-token-at-a-time mint-init, holders, and Metaplex scan on a shared RPC client."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import IntelligenceScan, Token
from app.services.creator_scan import clamp_creator_limit, scan_creator_tokens
from app.services.holder_owner_scan import scan_holder_owners
from app.services.metaplex_creator_scan import scan_metaplex_creators
from app.services.rpc_accounting import KIND_RATE_LIMITED, RpcAccounting, accounting_payload
from app.services.rpc_cooldown import cooldown_payload
from app.services.solana_rpc import SolanaRpcClient

logger = logging.getLogger("meme_hunter.creator_batch")

SCAN_TYPE = "creator_evidence_batch"
OPERATIONS = (
    ("mint_init", scan_creator_tokens),
    ("holders", scan_holder_owners),
    ("metaplex", scan_metaplex_creators),
)


@dataclass
class EvidenceOperationResult:
    name: str
    token_address: str | None
    status: str
    stop_reason: str | None = None
    rpc_calls: int = 0
    rpc_accounting: dict | None = None


@dataclass
class EvidenceBatchSummary:
    status: str
    stop_reason: str | None = None
    stop_token: str | None = None
    operations: list[EvidenceOperationResult] = field(default_factory=list)
    tokens_planned: list[str] = field(default_factory=list)
    tokens_started: list[str] = field(default_factory=list)
    tokens_skipped: list[str] = field(default_factory=list)
    rpc_calls: int = 0
    rpc_accounting: RpcAccounting | None = None
    cooldown: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    note: str = (
        "Mint initialization, holder ownership, and Metaplex metadata share one RPC client. "
        "RATE_LIMITED stops the batch. Remaining operations and tokens are skipped. "
        "This is not a successful batch if any operation was skipped. Paper trading only."
    )


async def run_creator_evidence_batch(
    session: Session,
    rpc: SolanaRpcClient,
    settings: Settings | None = None,
    token_address: str | None = None,
    limit: int | None = None,
) -> EvidenceBatchSummary:
    settings = settings or get_settings()
    applied = 1 if token_address else clamp_creator_limit(limit if limit is not None else 1, settings)
    summary = EvidenceBatchSummary(status="running", started_at=datetime.now(timezone.utc))
    scan_row = IntelligenceScan(scan_type=SCAN_TYPE, started_at=summary.started_at, status="running")
    session.add(scan_row)
    session.commit()

    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query.order_by(Token.last_discovered_at.desc(), Token.id.desc())).all())
    if not token_address:
        tokens = tokens[:applied]
    summary.tokens_planned = [token.token_address for token in tokens]

    halted = False
    for token in tokens:
        if halted or rpc.should_halt:
            summary.tokens_skipped.append(token.token_address)
            for name, _fn in OPERATIONS:
                summary.operations.append(
                    EvidenceOperationResult(
                        name=name,
                        token_address=token.token_address,
                        status="skipped",
                        stop_reason=rpc.halt_reason or KIND_RATE_LIMITED,
                    )
                )
            continue
        summary.tokens_started.append(token.token_address)
        for name, fn in OPERATIONS:
            if halted or rpc.should_halt:
                summary.operations.append(
                    EvidenceOperationResult(
                        name=name,
                        token_address=token.token_address,
                        status="skipped",
                        stop_reason=rpc.halt_reason or KIND_RATE_LIMITED,
                    )
                )
                continue
            result = await fn(session, rpc, settings, token_address=token.token_address, limit=1)
            op_status = result.status
            if op_status == "completed" and rpc.should_halt:
                op_status = "partial"
            summary.operations.append(
                EvidenceOperationResult(
                    name=name,
                    token_address=token.token_address,
                    status=op_status,
                    stop_reason=result.stop_reason,
                    rpc_calls=result.rpc_calls,
                    rpc_accounting=accounting_payload(result),
                )
            )
            if result.stop_reason or rpc.should_halt:
                halted = True
                summary.stop_reason = result.stop_reason or rpc.halt_reason or KIND_RATE_LIMITED
                summary.stop_token = result.stop_token or token.token_address

    accounting = rpc.accounting.copy()
    summary.rpc_accounting = accounting
    summary.rpc_calls = accounting.attempts
    summary.cooldown = cooldown_payload()
    if halted:
        summary.status = "partial"
    elif summary.status == "running":
        skipped = any(item.status == "skipped" for item in summary.operations)
        summary.status = "partial" if skipped else "completed"
    summary.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = summary.finished_at
    scan_row.status = summary.status
    scan_row.items_fetched = len(summary.tokens_planned)
    scan_row.items_saved = len(summary.tokens_started)
    scan_row.details_json = json.dumps(
        {
            "stop_reason": summary.stop_reason,
            "stop_token": summary.stop_token,
            "tokens_planned": summary.tokens_planned,
            "tokens_started": summary.tokens_started,
            "tokens_skipped": summary.tokens_skipped,
            "operations": [
                {
                    "name": item.name,
                    "token_address": item.token_address,
                    "status": item.status,
                    "stop_reason": item.stop_reason,
                    "rpc_calls": item.rpc_calls,
                }
                for item in summary.operations
            ],
            "rpc_calls": summary.rpc_calls,
            "rpc_accounting": accounting_payload(summary),
            "cooldown": summary.cooldown,
        }
    )
    scan_row.errors_json = json.dumps(summary.errors)
    session.commit()
    return summary
