"""Explainable 0-100 token risk score. Missing data is UNKNOWN, not safe."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.services.holder_analysis import HolderAnalysisResult
from app.services.liquidity_analysis import LiquidityAnalysisResult
from app.services.token_security import TokenSecurityResult
from app.services.trading_analysis import TradingAnalysisResult
from app.services.wallet_analysis import WalletAnalysisResult

INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
SCORED = "scored"


@dataclass
class CategoryScore:
    name: str
    weight: float
    score: float | None
    status: str
    reason: str


@dataclass
class RiskScoreResult:
    status: str
    observed_risk: float | None
    observed_risk_band: str | None
    risk_score: float | None
    risk_band: str | None
    confidence: float
    completeness: float
    categories: list[CategoryScore] = field(default_factory=list)
    unknown_categories: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    explanation: str = ""
    classification_reliable: bool = False


@dataclass
class ClassificationView:
    """Current-threshold classification over stored or freshly computed measurements."""

    status: str
    observed_risk: float | None
    observed_risk_band: str | None
    risk_score: float | None
    risk_band: str | None
    classification_reliable: bool


def classify_measurements(
    settings: Settings,
    *,
    completeness: float,
    confidence: float,
    observed_risk: float | None,
    observed_risk_band: str | None,
    stored_risk_score: float | None = None,
    stored_risk_band: str | None = None,
) -> ClassificationView:
    """Apply live thresholds. Legacy rows stored the observation in risk_score only."""

    observation = observed_risk if observed_risk is not None else stored_risk_score
    observation_band = observed_risk_band
    if observation_band is None:
        if observation is not None:
            observation_band = _band(observation)
        elif stored_risk_band:
            observation_band = stored_risk_band

    min_complete = settings.security_min_completeness
    min_confidence = settings.security_min_confidence
    meets_thresholds = completeness >= min_complete and confidence >= min_confidence
    if not meets_thresholds:
        status = INSUFFICIENT_DATA if completeness < min_complete else LOW_CONFIDENCE
        return ClassificationView(
            status=status,
            observed_risk=observation,
            observed_risk_band=observation_band,
            risk_score=None,
            risk_band=None,
            classification_reliable=False,
        )
    return ClassificationView(
        status=SCORED,
        observed_risk=observation,
        observed_risk_band=observation_band,
        risk_score=observation,
        risk_band=observation_band,
        classification_reliable=True,
    )


def score_token_risk(
    settings: Settings,
    security: TokenSecurityResult,
    holders: HolderAnalysisResult,
    liquidity: LiquidityAnalysisResult,
    trading: TradingAnalysisResult,
    wallet: WalletAnalysisResult,
) -> RiskScoreResult:
    categories = [
        _authorities(settings.risk_weight_authorities, security),
        _holders(settings.risk_weight_holders, holders),
        _liquidity(settings.risk_weight_liquidity, liquidity),
        _trading(settings.risk_weight_trading, trading),
        _deployer(settings.risk_weight_deployer, wallet),
    ]
    available = [item for item in categories if item.status != "UNKNOWN" and item.score is not None]
    unknown = [item.name for item in categories if item.status == "UNKNOWN"]
    completeness = round(sum(item.weight for item in available), 4)
    verified_weight = sum(item.weight for item in available if item.status == "VERIFIED")
    estimate_weight = sum(item.weight for item in available if item.status == "ESTIMATE")
    confidence = round(verified_weight + 0.5 * estimate_weight, 4)

    warnings: list[str] = []
    for item in categories:
        if item.status == "UNKNOWN":
            warnings.append(f"{item.name}: UNKNOWN — {item.reason}")
        elif item.score is not None and item.score >= 60:
            warnings.append(f"{item.name}: {item.reason}")

    observed_risk = None
    observed_band = None
    if available and completeness > 0:
        weighted = sum((item.score or 0) * item.weight for item in available)
        observed_risk = round(weighted / completeness, 1)
        observed_band = _band(observed_risk)

    classified = classify_measurements(
        settings,
        completeness=completeness,
        confidence=confidence,
        observed_risk=observed_risk,
        observed_risk_band=observed_band,
    )
    return RiskScoreResult(
        status=classified.status,
        observed_risk=classified.observed_risk,
        observed_risk_band=classified.observed_risk_band,
        risk_score=classified.risk_score,
        risk_band=classified.risk_band,
        confidence=confidence,
        completeness=completeness,
        categories=categories,
        unknown_categories=unknown,
        warnings=warnings,
        explanation=_explanation(
            classified,
            settings,
            completeness=completeness,
            confidence=confidence,
            unknown=unknown,
        ),
        classification_reliable=classified.classification_reliable,
    )


def _explanation(
    classified: ClassificationView,
    settings: Settings,
    *,
    completeness: float,
    confidence: float,
    unknown: list[str],
) -> str:
    min_complete = settings.security_min_completeness
    min_confidence = settings.security_min_confidence
    unknown_text = ", ".join(unknown) or "none"
    observed = classified.observed_risk
    band = classified.observed_risk_band
    if classified.status == INSUFFICIENT_DATA:
        return (
            f"INSUFFICIENT_DATA: completeness is below the {min_complete:.2f} threshold, so this "
            "is not a final risk classification. UNKNOWN categories are excluded from the "
            "weighted observation and are not treated as safe. Observed risk="
            f"{observed if observed is not None else 'n/a'}/100, "
            f"completeness={completeness:.2f}, confidence={confidence:.2f}. "
            f"Unknown: {unknown_text}."
        )
    if classified.status == LOW_CONFIDENCE:
        return (
            f"LOW_CONFIDENCE: completeness meets {min_complete:.2f}, but confidence is below "
            f"{min_confidence:.2f}, so this is not a confirmed classification. Treat observed "
            f"risk as provisional. Observed risk={observed}/100 ({band}). "
            f"Confidence={confidence:.2f}, completeness={completeness:.2f}. "
            f"Unknown: {unknown_text}."
        )
    return (
        f"Final assessment scored. Observed risk {observed}/100 ({band}). "
        "This is not a safety certification. "
        f"Confidence={confidence:.2f}, completeness={completeness:.2f}. "
        "Higher scores mean higher observed risk. UNKNOWN measurements were omitted."
    )


def _band(score: float) -> str:
    if score <= 19:
        return "lower"
    if score <= 39:
        return "moderate_low"
    if score <= 59:
        return "moderate"
    if score <= 79:
        return "high"
    return "critical"


def _authorities(weight: float, security: TokenSecurityResult) -> CategoryScore:
    if not security.available:
        return CategoryScore("token_authorities", weight, None, "UNKNOWN", "Mint account data unavailable")
    score = 10
    reasons = []
    if security.mint_authority_status == "VERIFIED_PRESENT":
        score += 45
        reasons.append("active mint authority")
    elif security.mint_authority_status == "UNKNOWN":
        return CategoryScore("token_authorities", weight, None, "UNKNOWN", "Mint authority not parsed")
    if security.freeze_authority_status == "VERIFIED_PRESENT":
        score += 30
        reasons.append("active freeze authority")
    elif security.freeze_authority_status == "UNKNOWN":
        reasons.append("freeze authority UNKNOWN")
        score += 10
    return CategoryScore(
        "token_authorities",
        weight,
        min(score, 100),
        "VERIFIED",
        "; ".join(reasons) or "mint and freeze authorities revoked on-chain",
    )


def _holders(weight: float, holders: HolderAnalysisResult) -> CategoryScore:
    if not holders.available or holders.concentration_status == "UNKNOWN":
        return CategoryScore("holder_concentration", weight, None, "UNKNOWN", "Holder percentages unavailable")
    if holders.concentration_status == "suspicious":
        score = 80
    elif holders.concentration_status == "elevated":
        score = 55
    else:
        score = 20
    return CategoryScore(
        "holder_concentration",
        weight,
        score,
        "VERIFIED",
        f"{holders.concentration_status} (LP/burn excluded when classified)",
    )


def _liquidity(weight: float, liquidity: LiquidityAnalysisResult) -> CategoryScore:
    if liquidity.liquidity_usd is None:
        return CategoryScore("liquidity_risk", weight, None, "UNKNOWN", "No stored liquidity measurement")
    score = 25
    reasons = [f"liquidity ${liquidity.liquidity_usd:.0f}"]
    if liquidity.liquidity_usd < 5000:
        score = 75
        reasons.append("very low liquidity")
    elif liquidity.liquidity_usd < 10000:
        score = 55
        reasons.append("below $10k preference")
    if liquidity.lock_status == "UNKNOWN":
        score = min(score + 15, 95)
        reasons.append("lock/burn UNKNOWN")
    if liquidity.liquidity_change_pct is not None and liquidity.liquidity_change_pct <= -40:
        score = min(score + 20, 100)
        reasons.append("recent liquidity drop")
    return CategoryScore("liquidity_risk", weight, score, "ESTIMATE", "; ".join(reasons))


def _trading(weight: float, trading: TradingAnalysisResult) -> CategoryScore:
    if trading.risk_hint is None:
        return CategoryScore("trading_behavior", weight, None, "UNKNOWN", "Not enough snapshots")
    return CategoryScore(
        "trading_behavior",
        weight,
        trading.risk_hint,
        "ESTIMATE",
        "Snapshot indicators only; not proof of manipulation",
    )


def _deployer(weight: float, wallet: WalletAnalysisResult) -> CategoryScore:
    if wallet.risk_hint is None:
        return CategoryScore("developer_activity", weight, None, "UNKNOWN", "Deployer/tx history unavailable")
    status = "ESTIMATE" if wallet.deployer_status == "ESTIMATE" else "VERIFIED"
    return CategoryScore(
        "developer_activity",
        weight,
        wallet.risk_hint,
        status,
        wallet.findings[0] if wallet.findings else wallet.deployer_status,
    )
