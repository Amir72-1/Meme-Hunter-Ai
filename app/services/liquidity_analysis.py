"""Liquidity risk from stored DexScreener snapshots. Locks are never assumed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models import MarketSnapshot, Token


@dataclass
class LiquidityAnalysisResult:
    liquidity_usd: float | None = None
    pool_address: str | None = None
    pool_age_hours: float | None = None
    liquidity_change_pct: float | None = None
    lock_status: str = "UNKNOWN"
    lock_evidence: str = (
        "Liquidity lock/burn was not independently verified. "
        "A pool existing is not evidence that LP tokens are locked or burned."
    )
    provider: str = "dexscreener_snapshots"
    findings: list[str] = field(default_factory=list)
    available: bool = False


def analyze_liquidity(
    token: Token,
    snapshots: list[MarketSnapshot],
    now: datetime | None = None,
) -> LiquidityAnalysisResult:
    result = LiquidityAnalysisResult(pool_address=token.pair_address)
    now = now or datetime.now(timezone.utc)
    ordered = sorted(snapshots, key=lambda item: item.recorded_at or now)
    if not ordered:
        result.findings.append("No stored market snapshots. Liquidity remains UNKNOWN.")
        result.lock_status = "UNKNOWN"
        return result

    latest = ordered[-1]
    result.available = latest.liquidity_usd is not None
    result.liquidity_usd = latest.liquidity_usd
    created = token.pair_created_at or latest.pair_created_at
    if created is not None:
        created_aware = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        result.pool_age_hours = max((now - created_aware).total_seconds() / 3600, 0)

    prior = [item.liquidity_usd for item in ordered[:-1] if item.liquidity_usd is not None]
    if latest.liquidity_usd is not None and prior:
        baseline = prior[-1]
        if baseline > 0:
            result.liquidity_change_pct = ((latest.liquidity_usd - baseline) / baseline) * 100
            if result.liquidity_change_pct <= -40:
                result.findings.append("Liquidity dropped 40%+ versus the previous snapshot (indicator).")

    result.lock_status = "UNKNOWN"
    result.findings.append(
        "Lock status is UNKNOWN: this engine does not treat DexScreener pools as locked liquidity."
    )
    if result.liquidity_usd is not None and result.liquidity_usd < 10000:
        result.findings.append("Observed liquidity is below the $10k screening preference.")
    return result
