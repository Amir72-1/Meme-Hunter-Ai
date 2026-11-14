"""Top-holder concentration with LP/burn classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError

BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


@dataclass
class ClassifiedHolder:
    address: str
    amount: float | None
    pct: float | None
    classification: str


@dataclass
class HolderAnalysisResult:
    top_holder_pct: float | None = None
    top5_pct: float | None = None
    top10_pct: float | None = None
    observed_accounts: int | None = None
    concentration_status: str = "UNKNOWN"
    holders: list[ClassifiedHolder] = field(default_factory=list)
    provider: str = "solana_rpc"
    errors: list[str] = field(default_factory=list)
    available: bool = False


async def analyze_holders(
    mint: str,
    rpc: SolanaRpcClient,
    supply: float | None,
    pair_address: str | None = None,
) -> HolderAnalysisResult:
    result = HolderAnalysisResult()
    try:
        accounts = await rpc.get_token_largest_accounts(mint)
    except SolanaRpcError as exc:
        result.errors.append(str(exc))
        result.concentration_status = "UNKNOWN"
        return result

    if not accounts:
        result.errors.append("No largest-account data returned")
        result.concentration_status = "UNKNOWN"
        return result

    result.available = True
    result.observed_accounts = len(accounts)
    classified: list[ClassifiedHolder] = []
    for account in accounts[:10]:
        address = str(account.get("address") or "")
        amount = _amount(account)
        pct = None
        if amount is not None and supply and supply > 0:
            pct = (amount / supply) * 100
        classified.append(
            ClassifiedHolder(
                address=address,
                amount=amount,
                pct=pct,
                classification=_classify(address, pair_address),
            )
        )
    result.holders = classified
    ordinary = [item for item in classified if item.classification == "unknown_holder"]
    if supply is None or supply <= 0:
        result.concentration_status = "UNKNOWN"
        result.errors.append("Supply unavailable; percentages are UNKNOWN.")
        return result
    if any(item.pct is None for item in classified):
        result.concentration_status = "UNKNOWN"
        return result

    ordinary_pcts = [item.pct or 0.0 for item in ordinary]
    raw_pcts = [item.pct or 0.0 for item in classified]
    result.top_holder_pct = max(ordinary_pcts) if ordinary_pcts else None
    result.top5_pct = sum(sorted(ordinary_pcts, reverse=True)[:5]) if ordinary_pcts else None
    result.top10_pct = sum(ordinary_pcts) if ordinary_pcts else sum(raw_pcts)
    top = result.top_holder_pct or 0
    top5 = result.top5_pct or 0
    if top >= 30 or top5 >= 70:
        result.concentration_status = "suspicious"
    elif top >= 15 or top5 >= 50:
        result.concentration_status = "elevated"
    else:
        result.concentration_status = "distributed"
    return result


def _classify(address: str, pair_address: str | None) -> str:
    if address in BURN_ADDRESSES:
        return "burn"
    if pair_address and address == pair_address:
        return "liquidity_pool"
    if not address:
        return "unknown_holder"
    return "unknown_holder"


def _amount(account: dict[str, Any]) -> float | None:
    ui = account.get("uiAmount")
    if ui is not None:
        try:
            return float(ui)
        except (TypeError, ValueError):
            pass
    amount = account.get("amount")
    try:
        return float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return None
