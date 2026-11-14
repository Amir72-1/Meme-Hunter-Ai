"""On-demand DexScreener quotes for shortlisted tokens. PAPER_ONLY; no universe polling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import PaperMarketQuote, Token
from app.services.dexscreener_client import DexScreenerClient, DexScreenerError, SOLANA_CHAIN_ID
from app.services.market_data import parse_epoch, select_best_pair, to_float
from app.services.solana_pda import is_valid_pubkey
from app.timeutil import as_utc, utc_now

logger = logging.getLogger("meme_hunter.paper_quotes")

SOURCE = "dexscreener_on_demand"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
FRESHNESS_PROVIDER = "PROVIDER_TIMESTAMP"
FRESHNESS_UNPROVEN = "RETRIEVAL_UNPROVEN"
PAIR_TRACKED = "TRACKED_PAIR"
PAIR_SELECTED = "SELECTED_PAIR"
PAIR_MISMATCH = "PAIR_MISMATCH"
PAIR_CHANGED = "PAIR_CHANGED"
PAIR_NOT_FOUND = "PAIR_NOT_FOUND"
PROVIDER_TS_KEYS = ("updatedAt", "priceTimestamp", "lastTradeTimestamp", "timestamp")

ClientFactory = Callable[..., DexScreenerClient]


class QuoteRequestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class PairSelection:
    pair: dict[str, Any] | None
    pair_address: str | None
    match_status: str
    reject_reason: str | None
    notes: str


class QuoteRefreshCoordinator:
    """In-flight dedup and short-lived HTTP cache for a single mint at a time."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def reset(self) -> None:
        self._cache.clear()
        self._locks.clear()

    async def fetch_pairs(
        self,
        mint: str,
        client: DexScreenerClient,
        cache_seconds: float,
    ) -> tuple[list[dict[str, Any]] | None, bool, DexScreenerError | None]:
        now = time.monotonic()
        cached = self._cache.get(mint)
        if cached is not None and cache_seconds > 0 and now - cached[0] < cache_seconds:
            return cached[1], True, None
        async with self._guard:
            lock = self._locks.get(mint)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[mint] = lock
        async with lock:
            now = time.monotonic()
            cached = self._cache.get(mint)
            if cached is not None and cache_seconds > 0 and now - cached[0] < cache_seconds:
                return cached[1], True, None
            try:
                pairs = await client.get_token_pairs(mint)
            except DexScreenerError as exc:
                return None, False, exc
            self._cache[mint] = (time.monotonic(), pairs)
            return pairs, False, None


_COORDINATOR = QuoteRefreshCoordinator()


def get_quote_coordinator() -> QuoteRefreshCoordinator:
    return _COORDINATOR


def reset_quote_coordinator() -> None:
    _COORDINATOR.reset()


def extract_provider_timestamp(pair: dict[str, Any]) -> tuple[datetime | None, str | None]:
    for key in PROVIDER_TS_KEYS:
        raw = pair.get(key)
        parsed = parse_epoch(raw)
        if parsed is not None:
            return parsed, str(raw)
    return None, None


def _pair_address(pair: dict[str, Any] | None) -> str | None:
    if not isinstance(pair, dict):
        return None
    value = pair.get("pairAddress")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _solana_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "").lower() != SOLANA_CHAIN_ID:
            continue
        selected.append(pair)
    return selected


def _find_pair(pairs: list[dict[str, Any]], pair_address: str) -> dict[str, Any] | None:
    wanted = pair_address.strip()
    for pair in pairs:
        if _pair_address(pair) == wanted:
            return pair
    return None


def select_identified_pair(
    pairs: list[dict[str, Any]],
    token: Token,
    previous: PaperMarketQuote | None,
) -> PairSelection:
    solana = _solana_pairs(pairs)
    tracked = (token.pair_address or "").strip() or None
    previous_pair = (previous.pair_address or "").strip() or None if previous is not None else None
    locked = tracked or previous_pair

    if locked:
        found = _find_pair(solana, locked)
        if found is None:
            best = select_best_pair(solana, token.token_address)
            other = _pair_address(best)
            reason = PAIR_MISMATCH if tracked else PAIR_CHANGED
            note = (
                f"Tracked pair {locked} was not in the DexScreener response. "
                "The quote service does not silently switch pairs."
            )
            if other and other != locked:
                note += f" A different pair {other} was present and ignored."
            return PairSelection(None, locked, reason, reason, note)
        status = PAIR_TRACKED if tracked else PAIR_SELECTED
        return PairSelection(found, locked, status, None, f"Using locked pair {locked}.")

    if not solana:
        return PairSelection(None, None, PAIR_NOT_FOUND, PAIR_NOT_FOUND, "No Solana pairs were returned.")

    best = select_best_pair(solana, token.token_address)
    address = _pair_address(best)
    if best is None or address is None:
        return PairSelection(None, None, PAIR_NOT_FOUND, PAIR_NOT_FOUND, "No identifiable Solana pair.")
    return PairSelection(best, address, PAIR_SELECTED, None, f"Selected pair {address}; no prior pair lock.")


def _latest_quote(session: Session, token_id: int) -> PaperMarketQuote | None:
    return session.scalar(
        select(PaperMarketQuote)
        .where(PaperMarketQuote.token_id == token_id)
        .order_by(PaperMarketQuote.retrieved_at.desc(), PaperMarketQuote.id.desc())
        .limit(1)
    )


def _latest_accepted_same_pair(
    session: Session,
    token_id: int,
    pair_address: str | None,
) -> PaperMarketQuote | None:
    if not pair_address:
        return None
    return session.scalar(
        select(PaperMarketQuote)
        .where(
            PaperMarketQuote.token_id == token_id,
            PaperMarketQuote.status == STATUS_ACCEPTED,
            PaperMarketQuote.pair_address == pair_address,
        )
        .order_by(PaperMarketQuote.retrieved_at.desc(), PaperMarketQuote.id.desc())
        .limit(1)
    )


def _discontinuity(
    previous: PaperMarketQuote | None,
    price: float | None,
    max_multiple: float,
) -> str | None:
    if previous is None or previous.price_usd is None or price is None:
        return None
    old = float(previous.price_usd)
    if old <= 0 or price <= 0:
        return None
    ratio = price / old if price >= old else old / price
    if ratio > max_multiple:
        return "SUSPICIOUS_DISCONTINUITY"
    return None


def _validate_market(
    price: float | None,
    liquidity: float | None,
    min_liquidity: float,
) -> str | None:
    if price is None:
        return "MISSING_PRICE"
    if price <= 0:
        return "INVALID_PRICE"
    if liquidity is None:
        return "MISSING_LIQUIDITY"
    if liquidity <= 0 or liquidity < min_liquidity:
        return "INSUFFICIENT_LIQUIDITY"
    return None


def _persist(
    session: Session,
    token: Token,
    *,
    retrieved_at: datetime,
    pair: dict[str, Any] | None,
    selection: PairSelection,
    previous: PaperMarketQuote | None,
    settings: Settings,
    cache_hit: bool,
    http_status: int | None,
    provider_error: str | None = None,
    force_status: str | None = None,
) -> PaperMarketQuote:
    price = to_float(pair.get("priceUsd")) if pair else None
    liq_obj = pair.get("liquidity") if pair else None
    liquidity = to_float(liq_obj.get("usd")) if isinstance(liq_obj, dict) else None
    market_cap = to_float(pair.get("marketCap")) if pair else None
    fdv = to_float(pair.get("fdv")) if pair else None
    dex_name = None
    if pair is not None and isinstance(pair.get("dexId"), str):
        dex_name = pair.get("dexId")
    market_observed_at, provider_raw = extract_provider_timestamp(pair) if pair else (None, None)
    freshness = FRESHNESS_PROVIDER if market_observed_at is not None else FRESHNESS_UNPROVEN
    reject = selection.reject_reason
    flag = None
    status = STATUS_REJECTED if reject else STATUS_ACCEPTED
    if reject is None:
        reject = _validate_market(price, liquidity, float(settings.paper_min_liquidity_usd))
        if reject:
            status = STATUS_REJECTED
        else:
            prior_same = _latest_accepted_same_pair(session, token.id, selection.pair_address)
            flag = _discontinuity(prior_same, price, float(settings.paper_quote_max_move_multiple))
            if flag:
                status = STATUS_REJECTED
                reject = flag
    if force_status:
        status = force_status
        if status == STATUS_PROVIDER_UNAVAILABLE:
            reject = reject or "PROVIDER_UNAVAILABLE"
            freshness = FRESHNESS_UNPROVEN
            price = None if price is None else price
    notes = selection.notes
    if provider_error:
        notes = f"{notes} {provider_error}".strip()
    if freshness == FRESHNESS_UNPROVEN and status == STATUS_ACCEPTED:
        notes = (
            f"{notes} Retrieval time is not proof that the underlying price is fresh."
        ).strip()
    details = {
        "http_cache_hit": cache_hit,
        "freshness_note": (
            "market_observed_at is the provider timestamp when present; "
            "retrieved_at is HTTP collection time and does not prove quote freshness."
        ),
        "price_field": "priceUsd",
        "liquidity_field": "liquidity.usd",
        "market_cap_used_as_price": False,
        "fdv_used_as_price": False,
    }
    row = PaperMarketQuote(
        token_id=token.id,
        mint_address=token.token_address,
        pair_address=selection.pair_address or _pair_address(pair),
        dex_name=dex_name,
        price_usd=price,
        liquidity_usd=liquidity,
        market_cap=market_cap,
        fdv=fdv,
        source=SOURCE,
        retrieved_at=retrieved_at,
        market_observed_at=as_utc(market_observed_at) if market_observed_at else None,
        provider_timestamp_raw=provider_raw,
        freshness_basis=freshness,
        status=status,
        reject_reason=reject,
        pair_match_status=selection.match_status,
        discontinuity_flag=flag,
        previous_quote_id=previous.id if previous is not None else None,
        cache_hit=1 if cache_hit else 0,
        http_status=http_status,
        notes=notes,
        details_json=json.dumps(details),
    )
    session.add(row)
    if (
        status == STATUS_ACCEPTED
        and not (token.pair_address or "").strip()
        and row.pair_address
    ):
        token.pair_address = row.pair_address
        if dex_name and not token.dex_name:
            token.dex_name = dex_name
    session.commit()
    session.refresh(row)
    return row


def load_catalog_token(session: Session, mint: str) -> Token:
    mint = mint.strip()
    if not is_valid_pubkey(mint):
        raise QuoteRequestError(422, "token_address must be a valid Solana public key")
    token = session.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise QuoteRequestError(404, "Token is not in the stored catalog")
    if str(token.chain or "").lower() != SOLANA_CHAIN_ID:
        raise QuoteRequestError(422, "Token is not recorded as a Solana mint")
    return token


def get_latest_stored_quote(session: Session, mint: str) -> PaperMarketQuote:
    token = load_catalog_token(session, mint)
    row = _latest_quote(session, token.id)
    if row is None:
        raise QuoteRequestError(404, "No on-demand paper quote has been stored for this token")
    return row


async def refresh_paper_quote(
    session: Session,
    mint: str,
    settings: Settings | None = None,
    *,
    client: DexScreenerClient | None = None,
    client_factory: ClientFactory | None = None,
) -> PaperMarketQuote:
    settings = settings or get_settings()
    token = load_catalog_token(session, mint)
    previous = _latest_quote(session, token.id)
    retrieved_at = utc_now()
    owns_client = client is None
    active = client
    factory = client_factory or DexScreenerClient
    try:
        if owns_client:
            active = factory(settings=settings)
            await active.__aenter__()
        assert active is not None
        pairs, cache_hit, error = await get_quote_coordinator().fetch_pairs(
            token.token_address,
            active,
            float(settings.paper_quote_cache_seconds),
        )
    except Exception:
        if owns_client and active is not None:
            await active.__aexit__(None, None, None)
        raise
    if owns_client and active is not None:
        await active.__aexit__(None, None, None)

    if error is not None:
        status_code = error.status_code
        if status_code == 404:
            selection = PairSelection(None, token.pair_address, PAIR_NOT_FOUND, PAIR_NOT_FOUND, str(error))
            return _persist(
                session,
                token,
                retrieved_at=retrieved_at,
                pair=None,
                selection=selection,
                previous=previous,
                settings=settings,
                cache_hit=False,
                http_status=404,
            )
        logger.warning("DexScreener unavailable for paper quote %s: %s", token.token_address, error)
        selection = PairSelection(
            None,
            token.pair_address,
            token.pair_address and PAIR_TRACKED or PAIR_NOT_FOUND,
            "PROVIDER_UNAVAILABLE",
            "DexScreener did not return a usable quote.",
        )
        return _persist(
            session,
            token,
            retrieved_at=retrieved_at,
            pair=None,
            selection=selection,
            previous=previous,
            settings=settings,
            cache_hit=False,
            http_status=status_code,
            provider_error=str(error),
            force_status=STATUS_PROVIDER_UNAVAILABLE,
        )

    selection = select_identified_pair(pairs or [], token, previous)
    http_status = 200
    return _persist(
        session,
        token,
        retrieved_at=retrieved_at,
        pair=selection.pair,
        selection=selection,
        previous=previous,
        settings=settings,
        cache_hit=cache_hit,
        http_status=http_status,
    )


def quote_to_public(row: PaperMarketQuote, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    now = as_utc(now or utc_now())
    retrieved = as_utc(row.retrieved_at)
    observed = as_utc(row.market_observed_at) if row.market_observed_at else None
    retrieval_age = max(0.0, (now - retrieved).total_seconds()) if retrieved else None
    quote_age = max(0.0, (now - observed).total_seconds()) if observed else None
    executable = paper_quote_is_executable(row, settings, now=now)
    return {
        "id": row.id,
        "token_id": row.token_id,
        "mint_address": row.mint_address,
        "pair_address": row.pair_address,
        "dex_name": row.dex_name,
        "price_usd": row.price_usd,
        "liquidity_usd": row.liquidity_usd,
        "market_cap": row.market_cap,
        "fdv": row.fdv,
        "source": row.source,
        "retrieved_at": retrieved,
        "market_observed_at": observed,
        "provider_timestamp_raw": row.provider_timestamp_raw,
        "freshness_basis": row.freshness_basis,
        "status": row.status,
        "reject_reason": row.reject_reason,
        "pair_match_status": row.pair_match_status,
        "discontinuity_flag": row.discontinuity_flag,
        "previous_quote_id": row.previous_quote_id,
        "cache_hit": bool(row.cache_hit),
        "http_status": row.http_status,
        "notes": row.notes,
        "retrieval_age_seconds": retrieval_age,
        "quote_age_seconds": quote_age,
        "executable": executable,
        "external_request": False,
        "note": (
            "On-demand DexScreener quote. retrieved_at is HTTP time. "
            "Executable paper fills require a provider market timestamp within "
            f"{float(settings.paper_max_quote_age_seconds):.0f}s plus valid price and liquidity. "
            "GET does not call DexScreener."
        ),
    }


def paper_quote_is_executable(
    row: PaperMarketQuote,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    if row.status != STATUS_ACCEPTED:
        return False
    if row.freshness_basis != FRESHNESS_PROVIDER or row.market_observed_at is None:
        return False
    if _validate_market(row.price_usd, row.liquidity_usd, float(settings.paper_min_liquidity_usd)):
        return False
    now = as_utc(now or utc_now())
    observed = as_utc(row.market_observed_at)
    if observed is None:
        return False
    age = max(0.0, (now - observed).total_seconds())
    return age <= float(settings.paper_max_quote_age_seconds)
