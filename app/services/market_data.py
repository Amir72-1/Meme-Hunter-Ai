"""Normalize DexScreener pair payloads. Missing fields stay None."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.services.dexscreener_client import SOLANA_CHAIN_ID

SOURCE_PROFILE = "profile"
SOURCE_BOOST_LATEST = "boost_latest"
SOURCE_BOOST_TOP = "boost_top"
SOURCE_STORED_REFRESH = "stored_refresh"


@dataclass
class DiscoveredAddress:
    token_address: str
    sources: set[str] = field(default_factory=set)
    active_boosts: int | None = None


@dataclass
class NormalizedMarket:
    token_address: str
    name: str | None
    symbol: str | None
    chain: str
    dex_name: str | None
    pair_address: str | None
    price_usd: float | None
    market_cap: float | None
    fdv: float | None
    liquidity_usd: float | None
    price_change_5m: float | None
    price_change_1h: float | None
    price_change_24h: float | None
    volume_5m: float | None
    volume_1h: float | None
    volume_24h: float | None
    buys_5m: int | None
    sells_5m: int | None
    buys_1h: int | None
    sells_1h: int | None
    buys_24h: int | None
    sells_24h: int | None
    pair_created_at: datetime | None
    active_boosts: int | None
    discovery_source: str
    filter_status: str
    filter_reasons: str
    snapshot_at: datetime


def extract_solana_addresses(
    profiles: list[dict[str, Any]],
    latest_boosts: list[dict[str, Any]],
    top_boosts: list[dict[str, Any]],
) -> dict[str, DiscoveredAddress]:
    discovered: dict[str, DiscoveredAddress] = {}
    _absorb_feed(discovered, profiles, SOURCE_PROFILE)
    _absorb_feed(discovered, latest_boosts, SOURCE_BOOST_LATEST, boost=True)
    _absorb_feed(discovered, top_boosts, SOURCE_BOOST_TOP, boost=True)
    return discovered


def _absorb_feed(
    discovered: dict[str, DiscoveredAddress],
    items: list[dict[str, Any]],
    source: str,
    boost: bool = False,
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        chain_id = str(item.get("chainId") or "").lower()
        if chain_id != SOLANA_CHAIN_ID:
            continue
        address = item.get("tokenAddress")
        if not isinstance(address, str) or not address.strip():
            continue
        key = address.strip()
        record = discovered.setdefault(key, DiscoveredAddress(token_address=key))
        record.sources.add(source)
        if boost:
            boost_value = to_int(item.get("totalAmount"))
            if boost_value is None:
                boost_value = to_int(item.get("amount"))
            if boost_value is not None:
                current = record.active_boosts or 0
                record.active_boosts = max(current, boost_value)


def group_pairs_by_token(pairs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "").lower() != SOLANA_CHAIN_ID:
            continue
        for token in (pair.get("baseToken"), pair.get("quoteToken")):
            if not isinstance(token, dict):
                continue
            address = token.get("address")
            if isinstance(address, str) and address.strip():
                grouped.setdefault(address.strip(), []).append(pair)
    return grouped


def select_best_pair(pairs: list[dict[str, Any]], token_address: str) -> dict[str, Any] | None:
    solana_pairs = [
        pair
        for pair in pairs
        if isinstance(pair, dict) and str(pair.get("chainId") or "").lower() == SOLANA_CHAIN_ID
    ]
    if not solana_pairs:
        return None

    def score(pair: dict[str, Any]) -> tuple[int, float]:
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        is_base = str(base.get("address") or "") == token_address
        liquidity = to_float(_nested(pair, "liquidity", "usd")) or 0.0
        volume = to_float(_timeframe(pair.get("volume"), "h24")) or 0.0
        txns = _txn_total(pair, "h24")
        txn_count = 0 if txns is None else txns
        return (1 if is_base else 0, liquidity + (volume * 0.25) + txn_count)

    return max(solana_pairs, key=score)


def normalize_pair(
    pair: dict[str, Any],
    discovered: DiscoveredAddress,
    settings: Settings,
    snapshot_at: datetime | None = None,
) -> NormalizedMarket:
    token_address = discovered.token_address
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    token_meta = base if str(base.get("address") or "") == token_address else quote
    if str(token_meta.get("address") or "") != token_address:
        token_meta = base or quote

    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    price_change = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
    boosts = pair.get("boosts") if isinstance(pair.get("boosts"), dict) else {}

    pair_boosts = to_int(boosts.get("active"))
    active_boosts = pair_boosts if pair_boosts is not None else discovered.active_boosts
    pair_created_at = parse_epoch(pair.get("pairCreatedAt"))
    buys_24h = _txn_side(txns, "h24", "buys")
    sells_24h = _txn_side(txns, "h24", "sells")
    volume_24h = to_float(_timeframe(volume, "h24"))
    liquidity_usd = to_float(_nested(pair, "liquidity", "usd"))
    filter_status, filter_reasons = classify_thresholds(
        liquidity_usd=liquidity_usd,
        volume_24h=volume_24h,
        pair_created_at=pair_created_at,
        txns_24h=_sum_optional(buys_24h, sells_24h),
        settings=settings,
        now=snapshot_at or datetime.now(timezone.utc),
    )
    sources = ",".join(sorted(discovered.sources)) or SOURCE_PROFILE

    return NormalizedMarket(
        token_address=token_address,
        name=_string(token_meta.get("name")),
        symbol=_string(token_meta.get("symbol")),
        chain=SOLANA_CHAIN_ID,
        dex_name=_string(pair.get("dexId")),
        pair_address=_string(pair.get("pairAddress")),
        price_usd=to_float(pair.get("priceUsd")),
        market_cap=to_float(pair.get("marketCap")),
        fdv=to_float(pair.get("fdv")),
        liquidity_usd=liquidity_usd,
        price_change_5m=to_float(_timeframe(price_change, "m5")),
        price_change_1h=to_float(_timeframe(price_change, "h1")),
        price_change_24h=to_float(_timeframe(price_change, "h24")),
        volume_5m=to_float(_timeframe(volume, "m5")),
        volume_1h=to_float(_timeframe(volume, "h1")),
        volume_24h=volume_24h,
        buys_5m=_txn_side(txns, "m5", "buys"),
        sells_5m=_txn_side(txns, "m5", "sells"),
        buys_1h=_txn_side(txns, "h1", "buys"),
        sells_1h=_txn_side(txns, "h1", "sells"),
        buys_24h=buys_24h,
        sells_24h=sells_24h,
        pair_created_at=pair_created_at,
        active_boosts=active_boosts,
        discovery_source=sources,
        filter_status=filter_status,
        filter_reasons=filter_reasons,
        snapshot_at=snapshot_at or datetime.now(timezone.utc),
    )


def classify_thresholds(
    liquidity_usd: float | None,
    volume_24h: float | None,
    pair_created_at: datetime | None,
    txns_24h: int | None,
    settings: Settings,
    now: datetime,
) -> tuple[str, str]:
    reasons: list[str] = []
    if liquidity_usd is None:
        reasons.append("liquidity_missing")
    elif liquidity_usd < settings.discovery_min_liquidity_usd:
        reasons.append("low_liquidity")

    if volume_24h is None:
        reasons.append("volume_missing")
    elif volume_24h < settings.discovery_min_volume_24h_usd:
        reasons.append("low_volume")

    if pair_created_at is not None:
        age_hours = (now - pair_created_at).total_seconds() / 3600
        if age_hours > settings.discovery_max_age_hours:
            reasons.append("older_than_preference")
    else:
        reasons.append("age_unknown")

    if txns_24h is None:
        reasons.append("txns_missing")
    elif txns_24h < settings.discovery_min_txns_24h:
        reasons.append("low_txns")

    if reasons:
        return "below_threshold", ",".join(reasons)
    return "meets_threshold", ""


def merge_discovery_sources(existing: str | None, incoming: str) -> str:
    parts = set()
    if existing:
        parts.update(item for item in existing.split(",") if item)
    parts.update(item for item in incoming.split(",") if item)
    return ",".join(sorted(parts))


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_epoch(value: Any) -> datetime | None:
    number = to_float(value)
    if number is None:
        return None
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _timeframe(payload: Any, key: str) -> Any:
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload.get(key)
    aliases = {"m5": ("m5", "5m"), "h1": ("h1", "1h"), "h24": ("h24", "24h")}
    for alias in aliases.get(key, (key,)):
        if alias in payload:
            return payload.get(alias)
    return None


def _txn_side(txns: Any, timeframe: str, side: str) -> int | None:
    if not isinstance(txns, dict):
        return None
    bucket = _timeframe(txns, timeframe)
    if not isinstance(bucket, dict):
        return None
    return to_int(bucket.get(side))


def _txn_total(pair: dict[str, Any], timeframe: str) -> int | None:
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    return _sum_optional(_txn_side(txns, timeframe, "buys"), _txn_side(txns, timeframe, "sells"))


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)
