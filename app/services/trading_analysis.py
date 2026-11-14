"""Trading-behavior indicators from stored snapshots. Indicators, not proof."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from app.models import MarketSnapshot


@dataclass
class TradingAnalysisResult:
    buy_sell_ratio: float | None = None
    volume_spike: bool | None = None
    liquidity_shock: bool | None = None
    volatility: float | None = None
    wash_trade_indicator: bool | None = None
    provider: str = "dexscreener_snapshots"
    findings: list[str] = field(default_factory=list)
    available: bool = False
    risk_hint: float | None = None


def analyze_trading(snapshots: list[MarketSnapshot]) -> TradingAnalysisResult:
    result = TradingAnalysisResult()
    ordered = sorted(snapshots, key=lambda item: item.recorded_at)
    if not ordered:
        result.findings.append("No snapshots available for trading analysis.")
        return result

    latest = ordered[-1]
    buys = latest.buys_24h
    sells = latest.sells_24h
    if buys is not None and sells is not None:
        result.available = True
        result.buy_sell_ratio = buys / max(sells, 1)
        if sells == 0 and buys > 20:
            result.findings.append("24h sells are zero while buys are elevated (indicator).")
        if buys == sells and (latest.volume_24h or 0) > 50000:
            result.wash_trade_indicator = True
            result.findings.append("Equal 24h buys and sells with high volume (possible wash-trade indicator).")
        else:
            result.wash_trade_indicator = False

    volumes = [item.volume_24h for item in ordered if item.volume_24h is not None]
    if latest.volume_24h is not None and len(volumes) >= 3:
        result.available = True
        prior = volumes[:-1]
        result.volume_spike = latest.volume_24h > (median(prior) * 3 if prior else latest.volume_24h)
        if result.volume_spike:
            result.findings.append("Latest 24h volume is 3x+ the median of prior snapshots (indicator).")
    elif latest.volume_24h is not None:
        result.volume_spike = None
        result.findings.append("Not enough snapshots to verify a volume spike.")

    liquids = [item.liquidity_usd for item in ordered if item.liquidity_usd is not None]
    if len(liquids) >= 2:
        change = liquids[-1] - liquids[-2]
        result.liquidity_shock = liquids[-2] > 0 and change / liquids[-2] <= -0.5
        if result.liquidity_shock:
            result.findings.append("Liquidity halved between the last two snapshots (indicator).")

    changes = [item.price_change_24h for item in ordered if item.price_change_24h is not None]
    if changes:
        result.available = True
        result.volatility = max(abs(value) for value in changes)
        if result.volatility >= 80:
            result.findings.append("24h price change magnitude is extreme (indicator).")

    hints = []
    if result.volume_spike:
        hints.append(55)
    if result.liquidity_shock:
        hints.append(70)
    if result.wash_trade_indicator:
        hints.append(60)
    if result.volatility is not None and result.volatility >= 80:
        hints.append(50)
    if hints:
        result.risk_hint = max(hints)
    elif result.available:
        result.risk_hint = 20
    return result
