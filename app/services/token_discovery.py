"""Solana discovery pipeline: fetch, normalize, upsert tokens, append snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DiscoveryScan, MarketSnapshot, Token, as_utc, utc_now
from app.services.dexscreener_client import DexScreenerClient, DexScreenerError
from app.services.market_data import (
    SOURCE_STORED_REFRESH,
    DiscoveredAddress,
    NormalizedMarket,
    extract_solana_addresses,
    group_pairs_by_token,
    merge_discovery_sources,
    normalize_pair,
    select_best_pair,
)

logger = logging.getLogger("meme_hunter.discovery")


@dataclass
class ScanIssue:
    stage: str
    detail: str
    token_address: str | None = None
    category: str | None = None
    retryable: bool | None = None


@dataclass
class DiscoveryScanResult:
    status: str
    profiles_fetched: int = 0
    latest_boosts_fetched: int = 0
    top_boosts_fetched: int = 0
    unique_solana_addresses: int = 0
    tokens_processed: int = 0
    snapshots_saved: int = 0
    stored_tokens_queued: int = 0
    below_threshold_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[ScanIssue] = field(default_factory=list)
    skipped: list[ScanIssue] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return self.status == "failed"


def _classify_error(exc: BaseException) -> tuple[str, bool]:
    text = str(exc).lower()
    if "getaddrinfo" in text or "11001" in text or "name or service not known" in text:
        return "dns", True
    if "timeout" in text or "deadline" in text:
        return "timeout", True
    if "rate limited" in text or "429" in text:
        return "rate_limit", True
    return "http", True


async def run_discovery_scan(
    session: Session,
    client: DexScreenerClient,
    settings: Settings | None = None,
) -> DiscoveryScanResult:
    settings = settings or get_settings()
    result = DiscoveryScanResult(status="running", started_at=datetime.now(timezone.utc))
    scan_row = DiscoveryScan(started_at=result.started_at, status="running")
    session.add(scan_row)
    session.commit()
    scan_id = scan_row.id
    deadline = settings.effective_scan_deadline_seconds()

    try:
        try:
            await asyncio.wait_for(_collect_scan(session, client, settings, result), timeout=deadline)
            session.commit()
            result.snapshots_saved = result.tokens_processed
            result.status = "completed"
        except asyncio.TimeoutError:
            session.rollback()
            result.snapshots_saved = 0
            result.tokens_processed = 0
            result.status = "failed"
            result.errors.append(
                ScanIssue(
                    stage="deadline",
                    detail=f"Scan exceeded hard deadline of {deadline}s (includes DNS and HTTP).",
                    category="deadline",
                    retryable=True,
                )
            )
        except DexScreenerError as exc:
            session.rollback()
            result.snapshots_saved = 0
            result.tokens_processed = 0
            result.status = "failed"
            category, retryable = _classify_error(exc)
            stage = "feeds" if result.profiles_fetched == 0 else "tokens"
            result.errors.append(
                ScanIssue(stage=stage, detail=str(exc), category=category, retryable=retryable)
            )
        except asyncio.CancelledError:
            session.rollback()
            result.snapshots_saved = 0
            result.tokens_processed = 0
            result.status = "interrupted"
            result.errors.append(
                ScanIssue(
                    stage="cancelled",
                    detail="Scan cancelled during shutdown or timeout.",
                    category="cancelled",
                    retryable=True,
                )
            )
            raise
        except Exception as exc:
            session.rollback()
            result.snapshots_saved = 0
            result.tokens_processed = 0
            result.status = "failed"
            result.errors.append(
                ScanIssue(
                    stage="unexpected",
                    detail=str(exc),
                    category="unexpected",
                    retryable=True,
                )
            )
            logger.exception("Discovery scan id=%s failed unexpectedly", scan_id)
    finally:
        try:
            row = session.get(DiscoveryScan, scan_id) or scan_row
            _finalize_scan_row(session, row, result)
        except Exception:
            logger.exception("Failed to finalize discovery scan id=%s", scan_id)
        duration = 0.0
        if result.started_at and result.finished_at:
            duration = (result.finished_at - result.started_at).total_seconds()
        issue = result.errors[0] if result.errors else None
        logger.info(
            "Discovery scan id=%s status=%s stage=%s category=%s duration_s=%.3f retryable=%s snapshots=%s",
            scan_id,
            result.status,
            issue.stage if issue else "none",
            issue.category if issue else "none",
            duration,
            issue.retryable if issue else None,
            result.snapshots_saved,
        )
    return result


async def _collect_scan(
    session: Session,
    client: DexScreenerClient,
    settings: Settings,
    result: DiscoveryScanResult,
) -> None:
    profiles = await client.get_latest_profiles()
    latest_boosts = await client.get_latest_boosts()
    top_boosts = await client.get_top_boosts()

    result.profiles_fetched = len(profiles)
    result.latest_boosts_fetched = len(latest_boosts)
    result.top_boosts_fetched = len(top_boosts)

    discovered = extract_solana_addresses(profiles, latest_boosts, top_boosts)
    result.unique_solana_addresses = len(discovered)
    result.stored_tokens_queued = queue_stored_tokens_for_refresh(session, discovered, settings)
    addresses = list(discovered.keys())

    pairs = await client.get_tokens(addresses) if addresses else []
    grouped = group_pairs_by_token(pairs)
    snapshot_at = datetime.now(timezone.utc)

    for address, meta in discovered.items():
        token_pairs = grouped.get(address, [])
        if not token_pairs:
            try:
                token_pairs = await client.get_token_pairs(address)
            except DexScreenerError as exc:
                category, retryable = _classify_error(exc)
                result.errors.append(
                    ScanIssue(
                        stage="token_pairs",
                        detail=str(exc),
                        token_address=address,
                        category=category,
                        retryable=retryable,
                    )
                )
                continue
        _process_address(
            session,
            address,
            meta,
            token_pairs,
            settings,
            snapshot_at,
            result,
        )


def _process_address(
    session: Session,
    address: str,
    meta: DiscoveredAddress,
    token_pairs: list[dict],
    settings: Settings,
    snapshot_at: datetime,
    result: DiscoveryScanResult,
) -> None:
    best_pair = select_best_pair(token_pairs, address)
    if best_pair is None:
        result.skipped.append(
            ScanIssue(
                stage="pair_select",
                detail="No usable Solana pair",
                token_address=address,
            )
        )
        return

    market = normalize_pair(best_pair, meta, settings, snapshot_at=snapshot_at)
    upsert_token(session, market)
    insert_snapshot(session, market)
    result.tokens_processed += 1
    if market.filter_status == "below_threshold":
        result.below_threshold_count += 1


def queue_stored_tokens_for_refresh(
    session: Session,
    discovered: dict[str, DiscoveredAddress],
    settings: Settings,
) -> int:
    """Append current DexScreener observations for tokens already in the database.

    Feed lists rotate. Without this, previously discovered tokens never get a second snapshot.
    """

    if not settings.discovery_refresh_stored_tokens:
        return 0
    limit = max(int(settings.discovery_refresh_stored_limit), 0)
    if limit == 0:
        return 0
    stored = list(
        session.scalars(
            select(Token.token_address)
            .order_by(Token.last_discovered_at.desc(), Token.id.desc())
            .limit(limit)
        ).all()
    )
    added = 0
    for address in stored:
        if address in discovered:
            continue
        discovered[address] = DiscoveredAddress(token_address=address, sources={SOURCE_STORED_REFRESH})
        added += 1
    return added


def upsert_token(session: Session, market: NormalizedMarket) -> Token:
    token = session.scalar(select(Token).where(Token.token_address == market.token_address))
    now = utc_now()
    if token is None:
        token = Token(
            token_address=market.token_address,
            chain=market.chain,
            created_at=now,
        )
        session.add(token)

    token.name = market.name
    token.symbol = market.symbol
    token.chain = market.chain
    token.dex_name = market.dex_name
    token.pair_address = market.pair_address
    token.pair_created_at = market.pair_created_at
    token.discovery_source = merge_discovery_sources(token.discovery_source, market.discovery_source)
    token.last_discovered_at = market.snapshot_at
    token.filter_status = market.filter_status
    token.filter_reasons = market.filter_reasons
    token.updated_at = now
    session.flush()
    return token


def insert_snapshot(session: Session, market: NormalizedMarket) -> MarketSnapshot:
    """Append a new observation. Never updates or deletes prior snapshots."""
    token = session.scalar(select(Token).where(Token.token_address == market.token_address))
    if token is None:
        raise RuntimeError(f"Token {market.token_address} must be upserted before snapshot insert")

    snapshot = MarketSnapshot(
        token_id=token.id,
        price_usd=market.price_usd,
        market_cap=market.market_cap,
        liquidity_usd=market.liquidity_usd,
        volume_24h=market.volume_24h,
        volume_5m=market.volume_5m,
        volume_1h=market.volume_1h,
        price_change_5m=market.price_change_5m,
        price_change_1h=market.price_change_1h,
        price_change_24h=market.price_change_24h,
        fdv=market.fdv,
        buys_5m=market.buys_5m,
        sells_5m=market.sells_5m,
        buys_1h=market.buys_1h,
        sells_1h=market.sells_1h,
        buys_24h=market.buys_24h,
        sells_24h=market.sells_24h,
        dex_name=market.dex_name,
        pair_address=market.pair_address,
        pair_created_at=market.pair_created_at,
        active_boosts=market.active_boosts,
        discovery_source=market.discovery_source,
        filter_status=market.filter_status,
        filter_reasons=market.filter_reasons,
        recorded_at=as_utc(market.snapshot_at) or utc_now(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def latest_scan(session: Session) -> DiscoveryScan | None:
    return session.scalar(select(DiscoveryScan).order_by(DiscoveryScan.started_at.desc()).limit(1))


def _finalize_scan_row(session: Session, scan_row: DiscoveryScan, result: DiscoveryScanResult) -> None:
    result.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = result.finished_at
    scan_row.status = result.status
    scan_row.profiles_fetched = result.profiles_fetched
    scan_row.latest_boosts_fetched = result.latest_boosts_fetched
    scan_row.top_boosts_fetched = result.top_boosts_fetched
    scan_row.unique_solana_addresses = result.unique_solana_addresses
    scan_row.tokens_processed = result.tokens_processed
    scan_row.snapshots_saved = result.snapshots_saved
    scan_row.below_threshold_count = result.below_threshold_count
    scan_row.errors_json = _issues_json(result.errors)
    scan_row.skipped_json = _issues_json(result.skipped)
    session.commit()


def _issues_json(issues: list[ScanIssue]) -> str:
    return json.dumps(
        [
            {
                "stage": issue.stage,
                "detail": issue.detail,
                "token_address": issue.token_address,
                "category": issue.category,
                "retryable": issue.retryable,
            }
            for issue in issues
        ]
    )
