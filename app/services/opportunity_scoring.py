"""Explainable opportunity ranking from stored market, narrative, and security data."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")

from app.config import Settings
from app.models import (
    LiquidityAnalysisRecord,
    MarketSnapshot,
    Narrative,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
)
from app.services.risk_scoring import classify_measurements

SCORED = "SCORED"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

HIGH_OPPORTUNITY = "HIGH_OPPORTUNITY"
WATCHLIST = "WATCHLIST"
LOW_OPPORTUNITY = "LOW_OPPORTUNITY"

MARKET = "market_momentum"
NARRATIVE = "narrative_momentum"
DISCOVERY = "early_discovery"
LIQUIDITY = "liquidity_quality"
SECURITY = "security_adjustment"

CONFIRMED_MATCH_METHODS = {"symbol_and_source", "name_and_source"}
SECURITY_COVERAGE_WEIGHT = 0.20


@dataclass
class ComponentResult:
    name: str
    weight: float
    score: float | None
    confidence: float
    status: str
    explanation: str
    signals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, dict] = field(default_factory=dict)


@dataclass
class OpportunityResult:
    status: str
    ranking_label: str
    opportunity_score: float | None
    positive_opportunity_score: float | None
    security_adjustment: float
    confidence: float
    data_completeness: float
    available_components: list[str]
    missing_components: list[str]
    components: list[ComponentResult]
    positive_signals: list[str]
    negative_signals: list[str]
    warnings: list[str]
    explanation: str
    assessed_at: datetime
    snapshot_count: int = 0
    last_snapshot_at: datetime | None = None
    snapshot_age_hours: float | None = None
    narrative_match_count: int = 0
    security_coverage: str = "UNAVAILABLE"


def positive_weights(settings: Settings) -> dict[str, float]:
    return {
        MARKET: settings.opportunity_weight_momentum,
        NARRATIVE: settings.opportunity_weight_narrative,
        DISCOVERY: settings.opportunity_weight_discovery,
        LIQUIDITY: settings.opportunity_weight_liquidity,
    }


def normalize_positive_score(components: list[ComponentResult]) -> float | None:
    measured = [item for item in components if item.score is not None and item.weight > 0]
    total_weight = sum(item.weight for item in measured)
    if not measured or total_weight <= 0:
        return None
    weighted = sum((item.score or 0) * item.weight for item in measured)
    return round(weighted / total_weight, 1)


def assess_opportunity(
    settings: Settings,
    token: Token,
    snapshots: list[MarketSnapshot],
    matches: list[tuple[TokenNarrativeMatch, Narrative | None]],
    risk: TokenRiskAssessment | None,
    liquidity_record: LiquidityAnalysisRecord | None,
    now: datetime | None = None,
) -> OpportunityResult:
    now = now or datetime.now(timezone.utc)
    snapshots = snapshots_as_of(snapshots, now)
    weights = positive_weights(settings)
    market = score_market_momentum(snapshots, settings, now, weights[MARKET])
    narrative = score_narrative_momentum(matches, settings, weights[NARRATIVE])
    discovery = score_early_discovery(token, snapshots, settings, now, weights[DISCOVERY])
    liquidity = score_liquidity_quality(snapshots, liquidity_record, settings, now, weights[LIQUIDITY])
    security = score_security_adjustment(risk, settings)

    positives = [market, narrative, discovery, liquidity]
    available = [item.name for item in positives if item.score is not None]
    missing = [item.name for item in positives if item.score is None]
    if security.status == "UNAVAILABLE":
        missing.append(SECURITY)
    else:
        available.append(SECURITY)

    positive_score = normalize_positive_score(positives)
    adjustment = security.score if security.score is not None else 0.0
    opportunity_score = None
    if positive_score is not None:
        opportunity_score = round(max(0.0, min(100.0, positive_score + adjustment)), 1)

    positive_weight_total = sum(weights.values())
    coverage = sum(item.weight for item in positives if item.score is not None)
    if security.status != "UNAVAILABLE":
        coverage += SECURITY_COVERAGE_WEIGHT
    completeness = round(coverage / (positive_weight_total + SECURITY_COVERAGE_WEIGHT), 4)

    confidence_mass = sum(item.confidence * item.weight for item in positives if item.score is not None)
    if security.status != "UNAVAILABLE":
        confidence_mass += security.confidence * SECURITY_COVERAGE_WEIGHT
    confidence = round(confidence_mass / (positive_weight_total + SECURITY_COVERAGE_WEIGHT), 4)

    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=positive_score,
        opportunity_score=opportunity_score,
        data_completeness=completeness,
        confidence=confidence,
    )

    warnings: list[str] = []
    positive_signals: list[str] = []
    negative_signals: list[str] = []
    for item in [*positives, security]:
        warnings.extend(item.warnings)
        if item.score is None:
            continue
        if item.name == SECURITY:
            if item.score < 0:
                negative_signals.extend(item.signals)
            else:
                warnings.extend(item.signals)
        elif item.score >= 60:
            positive_signals.extend(item.signals)
        elif item.score <= 35:
            negative_signals.extend(item.signals)
        else:
            positive_signals.extend(item.signals)

    ordered_snaps = sorted(
        [item for item in snapshots if item.recorded_at is not None],
        key=lambda item: _aware(item.recorded_at) or now,
    )
    last_snapshot_at = _aware(ordered_snaps[-1].recorded_at) if ordered_snaps else None
    snapshot_age_hours = None
    if last_snapshot_at is not None:
        snapshot_age_hours = round(max((now - last_snapshot_at).total_seconds() / 3600, 0), 4)
    explanation = _explanation(
        settings,
        status=status,
        label=label,
        opportunity_score=opportunity_score,
        positive_score=positive_score,
        adjustment=adjustment,
        completeness=completeness,
        confidence=confidence,
        missing=missing,
    )
    return OpportunityResult(
        status=status,
        ranking_label=label,
        opportunity_score=opportunity_score,
        positive_opportunity_score=positive_score,
        security_adjustment=adjustment,
        confidence=confidence,
        data_completeness=completeness,
        available_components=available,
        missing_components=missing,
        components=[*positives, security],
        positive_signals=positive_signals,
        negative_signals=negative_signals,
        warnings=warnings,
        explanation=explanation,
        assessed_at=now,
        snapshot_count=len(ordered_snaps),
        last_snapshot_at=last_snapshot_at,
        snapshot_age_hours=snapshot_age_hours,
        narrative_match_count=len(matches),
        security_coverage=security.status,
    )


def classify_opportunity(
    settings: Settings,
    *,
    positive_opportunity_score: float | None,
    opportunity_score: float | None,
    data_completeness: float,
    confidence: float,
) -> tuple[str, str]:
    """Apply live completeness/confidence floors. Observed scores may still be returned."""

    if positive_opportunity_score is None:
        status = INSUFFICIENT_DATA
    elif data_completeness < settings.opportunity_min_completeness:
        status = INSUFFICIENT_DATA
    elif confidence < settings.opportunity_min_confidence:
        status = LOW_CONFIDENCE
    else:
        status = SCORED
    return status, ranking_label(status, opportunity_score, confidence, settings)


def ranking_label(
    status: str,
    opportunity_score: float | None,
    confidence: float,
    settings: Settings,
) -> str:
    if status == INSUFFICIENT_DATA or opportunity_score is None:
        return INSUFFICIENT_DATA
    if (
        status == SCORED
        and opportunity_score >= settings.opportunity_high_threshold
        and confidence >= settings.opportunity_min_confidence
    ):
        return HIGH_OPPORTUNITY
    if opportunity_score >= settings.opportunity_watch_threshold:
        return WATCHLIST
    return LOW_OPPORTUNITY


def snapshots_as_of(snapshots: list[MarketSnapshot], now: datetime) -> list[MarketSnapshot]:
    """Keep observations at or before as-of time. Future rows are ignored."""

    kept: list[MarketSnapshot] = []
    for item in snapshots:
        stamp = _aware(item.recorded_at)
        if stamp is None or stamp <= now:
            kept.append(item)
    return kept


def observation_time(row: object) -> datetime | None:
    """Prefer observed_at; fall back to scanned_at for legacy rows."""

    stamp = _aware(getattr(row, "observed_at", None))
    if stamp is not None:
        return stamp
    return _aware(getattr(row, "scanned_at", None))


def observations_as_of(rows: list[T], now: datetime) -> list[T]:
    """Keep stored observations with observed_at (or scanned_at) at or before T."""

    cutoff = _aware(now)
    if cutoff is None:
        return []
    kept: list[T] = []
    for row in rows:
        stamp = observation_time(row)
        if stamp is not None and stamp <= cutoff:
            kept.append(row)
    return kept


def analyze_market_history(
    snapshots: list[MarketSnapshot],
    now: datetime,
    stale_hours: float,
) -> dict[str, dict | int | float | str | None]:
    """Describe stored snapshot history. Growth requires two valid observations."""

    ordered = sorted(
        [item for item in snapshots if item.recorded_at is not None],
        key=lambda item: _aware(item.recorded_at) or now,
    )
    count = len(ordered)
    last_at = _aware(ordered[-1].recorded_at) if ordered else None
    age_hours = None
    if last_at is not None:
        age_hours = round(max((now - last_at).total_seconds() / 3600, 0), 4)

    def unavailable(reason: str) -> dict:
        return {"status": "UNAVAILABLE", "value": None, "reason": reason}

    if count == 0:
        reason = "No stored market snapshots."
        return {
            "snapshot_count": 0,
            "last_snapshot_at": None,
            "snapshot_age_hours": None,
            "stale": False,
            "price_change": unavailable(reason),
            "liquidity_change": unavailable(reason),
            "volume_change": unavailable(reason),
            "txn_growth": unavailable(reason),
            "acceleration": unavailable(reason),
        }
    if count < 2:
        reason = (
            f"Only {count} stored snapshot; successive observations are required to compute growth."
        )
        return {
            "snapshot_count": count,
            "last_snapshot_at": last_at.isoformat() if last_at else None,
            "snapshot_age_hours": age_hours,
            "stale": bool(age_hours is not None and age_hours > stale_hours),
            "price_change": unavailable(reason),
            "liquidity_change": unavailable(reason),
            "volume_change": unavailable(reason),
            "txn_growth": unavailable(reason),
            "acceleration": unavailable("Acceleration needs at least three timestamped snapshots."),
        }

    latest, previous = ordered[-1], ordered[-2]
    price = _safe_growth(latest.price_usd, previous.price_usd)
    liquidity = _safe_growth(latest.liquidity_usd, previous.liquidity_usd)
    volume = _safe_growth(latest.volume_24h, previous.volume_24h)
    latest_tx = _txn_count(latest)
    previous_tx = _txn_count(previous)
    txns = _safe_growth(latest_tx, previous_tx)
    accel_value = None
    accel_reason = "Acceleration needs at least three timestamped snapshots."
    if count >= 3:
        older = ordered[-3]
        recent_g = _safe_growth(latest.volume_24h, previous.volume_24h)
        prior_g = _safe_growth(previous.volume_24h, older.volume_24h)
        if recent_g is None or prior_g is None:
            accel_reason = "Volume was missing or zero in an earlier snapshot, so acceleration was not computed."
        else:
            accel_value = round(recent_g - prior_g, 4)
            accel_reason = "Volume growth change across the last three snapshots."

    def measured_or_not(value: float | None, missing_reason: str) -> dict:
        if value is None:
            return unavailable(missing_reason)
        return {"status": "MEASURED", "value": value, "reason": "Computed from successive stored snapshots."}

    return {
        "snapshot_count": count,
        "last_snapshot_at": last_at.isoformat() if last_at else None,
        "snapshot_age_hours": age_hours,
        "stale": bool(age_hours is not None and age_hours > stale_hours),
        "price_change": measured_or_not(price, "Price was missing or previous price was zero/invalid."),
        "liquidity_change": measured_or_not(liquidity, "Liquidity was missing or previous liquidity was zero/invalid."),
        "volume_change": measured_or_not(volume, "Volume was missing or previous volume was zero/invalid."),
        "txn_growth": measured_or_not(txns, "Buy/sell counts were missing or previous txn count was zero/invalid."),
        "acceleration": (
            {"status": "MEASURED", "value": accel_value, "reason": accel_reason}
            if accel_value is not None
            else unavailable(accel_reason)
        ),
    }


def score_market_momentum(
    snapshots: list[MarketSnapshot],
    settings: Settings,
    now: datetime,
    weight: float,
) -> ComponentResult:
    history = analyze_market_history(snapshots, now, settings.opportunity_stale_snapshot_hours)
    warnings: list[str] = []
    signals: list[str] = []
    count = int(history["snapshot_count"] or 0)
    if count < 2:
        reason = (
            "Market momentum is UNAVAILABLE: a single snapshot cannot establish growth, "
            "acceleration, or buy/sell transaction growth. DexScreener 24h change fields are "
            "not used as a substitute for local history."
        )
        if count == 1:
            warnings.append(reason)
        return ComponentResult(
            MARKET,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            reason if count else "No stored market snapshots. Momentum was not measured.",
            warnings=warnings,
            metrics=history,  # type: ignore[arg-type]
        )

    parts: list[float] = []
    for key, mapper in (
        ("price_change", _map_growth),
        ("liquidity_change", _map_growth),
        ("volume_change", _map_growth),
        ("txn_growth", _map_growth),
    ):
        metric = history[key]
        if isinstance(metric, dict) and metric.get("status") == "MEASURED" and metric.get("value") is not None:
            value = float(metric["value"])
            parts.append(mapper(value))
            signals.append(f"{key} {value:.1%} across snapshots")
    accel = history["acceleration"]
    if isinstance(accel, dict) and accel.get("status") == "MEASURED" and accel.get("value") is not None:
        accel_value = float(accel["value"])
        parts.append(_clip(50 + accel_value * 40, 0, 100))
        signals.append(f"volume acceleration delta {accel_value:.2f}")

    if history.get("stale"):
        warnings.append(
            f"Latest snapshot is {history.get('snapshot_age_hours')}h old "
            f"(stale threshold {settings.opportunity_stale_snapshot_hours:.0f}h)."
        )
    if not parts:
        return ComponentResult(
            MARKET,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "At least two snapshots exist, but price, liquidity, volume, and txn growth were UNAVAILABLE.",
            warnings=warnings,
            metrics=history,  # type: ignore[arg-type]
        )

    score = round(sum(parts) / len(parts), 1)
    confidence = 0.72
    if count >= 3 and isinstance(accel, dict) and accel.get("status") == "MEASURED":
        confidence = min(0.85, confidence + 0.08)
    if history.get("stale"):
        confidence *= 0.45
    return ComponentResult(
        MARKET,
        weight,
        score,
        round(confidence, 4),
        "MEASURED",
        "; ".join(signals),
        signals=signals,
        warnings=warnings,
        metrics=history,  # type: ignore[arg-type]
    )


def score_narrative_momentum(
    matches: list[tuple[TokenNarrativeMatch, Narrative | None]],
    settings: Settings,
    weight: float,
) -> ComponentResult:
    if not matches:
        return ComponentResult(
            NARRATIVE,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "No stored token-to-narrative matches. Missing narrative data is not treated as zero social activity. "
            "Matches are stored only when a token symbol/name overlaps a narrative keyword (and usually a stored news source). "
            "Unconfigured Reddit/X/Telegram adapters do not invent social posts.",
        )

    usable: list[tuple[float, float, str, str]] = []
    warnings: list[str] = []
    signals: list[str] = []
    for match, narrative in matches:
        if match.confidence < settings.narrative_match_min_confidence:
            warnings.append(
                f"Match to '{getattr(narrative, 'name', 'narrative')}' is below the configured match confidence floor."
            )
            continue
        if match.method == "name_keyword_hypothesis":
            warnings.append(
                "Name/keyword overlap alone is not treated as a confirmed narrative connection."
            )
            continue
        trend = 50.0 if narrative is None or narrative.trend_score is None else float(narrative.trend_score)
        linked = trend * match.confidence
        usable.append((linked, match.confidence, match.method, getattr(narrative, "name", "narrative")))
        extra = []
        if narrative is not None:
            if narrative.mention_growth is not None:
                extra.append(f"mention growth {narrative.mention_growth:.2f}")
            if narrative.engagement_growth is not None:
                extra.append(f"engagement growth {narrative.engagement_growth:.2f}")
            if narrative.independent_sources is not None:
                extra.append(f"{narrative.independent_sources} independent sources")
        prefix = "confirmed source-backed match" if match.method in CONFIRMED_MATCH_METHODS else "keyword overlap match"
        signals.append(f"{prefix} '{usable[-1][3]}' (confidence {match.confidence:.2f})" + ("; " + ", ".join(extra) if extra else ""))
        if match.method not in CONFIRMED_MATCH_METHODS:
            warnings.append(
                "Narrative link is not source-backed; it is not treated as confirmed evidence."
            )

    if not usable:
        return ComponentResult(
            NARRATIVE,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "Stored matches exist but none meet the evidence bar for narrative momentum.",
            warnings=warnings,
        )

    best_score, best_conf, _, _ = max(usable, key=lambda item: item[0])
    return ComponentResult(
        NARRATIVE,
        weight,
        round(_clip(best_score, 0, 100), 1),
        round(_clip(best_conf, 0, 1), 4),
        "MEASURED" if any(item[2] in CONFIRMED_MATCH_METHODS for item in usable) else "ESTIMATE",
        "; ".join(signals),
        signals=signals,
        warnings=warnings,
    )


def score_early_discovery(
    token: Token,
    snapshots: list[MarketSnapshot],
    settings: Settings,
    now: datetime,
    weight: float,
) -> ComponentResult:
    pair_at = _aware(token.pair_created_at)
    if pair_at is None:
        for snap in snapshots:
            pair_at = _aware(snap.pair_created_at)
            if pair_at is not None:
                break
    discovered_at = _aware(token.last_discovered_at) or _aware(token.created_at)
    if pair_at is None and discovered_at is None:
        return ComponentResult(
            DISCOVERY,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "No pair creation or discovery timestamp is stored.",
        )

    origin = pair_at or discovered_at
    age_hours = max((now - origin).total_seconds() / 3600, 0) if origin else None
    latest_liq = None
    ordered = sorted(
        [item for item in snapshots if item.recorded_at is not None],
        key=lambda item: _aware(item.recorded_at) or now,
    )
    if ordered and ordered[-1].liquidity_usd is not None:
        latest_liq = ordered[-1].liquidity_usd
    volume_rising = False
    if len(ordered) >= 2:
        growth = _safe_growth(ordered[-1].volume_24h, ordered[-2].volume_24h)
        volume_rising = growth is not None and growth > 0

    if age_hours is None:
        age_score = 40.0
    elif age_hours < 1:
        age_score = 48.0
    elif age_hours <= 36:
        age_score = 82.0
    elif age_hours <= 72:
        age_score = 62.0
    elif age_hours <= 168:
        age_score = 38.0
    else:
        age_score = 18.0

    snapshot_bonus = min(len(ordered) * 2, 8)
    activity_bonus = 8 if volume_rising else 0
    score = age_score + snapshot_bonus * 0.4 + activity_bonus
    warnings: list[str] = []
    signals = [
        f"pair/discovery age {age_hours:.1f}h" if age_hours is not None else "age unknown",
        f"{len(ordered)} stored snapshot(s)",
    ]
    if volume_rising:
        signals.append("market activity increased across snapshots")
    if age_hours is not None and age_hours < 6 and (latest_liq is None or latest_liq < 5000):
        score = min(score, 35)
        warnings.append(
            "Newly created token with thin or unknown liquidity is not ranked as an automatic early opportunity."
        )
        signals.append("thin/unknown liquidity cap applied")
    confidence = 0.55 if pair_at is not None else 0.4
    if len(ordered) >= 2:
        confidence += 0.1
    return ComponentResult(
        DISCOVERY,
        weight,
        round(_clip(score, 0, 100), 1),
        round(_clip(confidence, 0, 1), 4),
        "MEASURED" if pair_at is not None else "ESTIMATE",
        "; ".join(signals),
        signals=signals,
        warnings=warnings,
    )


def score_liquidity_quality(
    snapshots: list[MarketSnapshot],
    liquidity_record: LiquidityAnalysisRecord | None,
    settings: Settings,
    now: datetime,
    weight: float,
) -> ComponentResult:
    ordered = sorted(
        [item for item in snapshots if item.recorded_at is not None],
        key=lambda item: _aware(item.recorded_at) or now,
    )
    latest_liq = ordered[-1].liquidity_usd if ordered else None
    if latest_liq is None and liquidity_record is not None:
        latest_liq = liquidity_record.liquidity_usd
    if latest_liq is None:
        return ComponentResult(
            LIQUIDITY,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "No stored liquidity_usd measurement.",
        )

    if latest_liq <= 0:
        amount_score = 0.0
    elif latest_liq < 5000:
        amount_score = 25.0
    elif latest_liq < settings.discovery_min_liquidity_usd:
        amount_score = 45.0
    elif latest_liq < 25000:
        amount_score = 68.0
    elif latest_liq < 50000:
        amount_score = 82.0
    else:
        amount_score = 90.0

    change_score = None
    signals = [f"liquidity ${latest_liq:,.0f}"]
    warnings: list[str] = []
    if len(ordered) >= 2:
        change = _safe_growth(ordered[-1].liquidity_usd, ordered[-2].liquidity_usd)
        if change is not None:
            change_score = _clip(55 + change * 50, 0, 100)
            signals.append(f"liquidity change {change:.1%} across snapshots")
            if change <= -0.4:
                warnings.append("Liquidity dropped sharply across snapshots.")
    elif liquidity_record is not None and liquidity_record.liquidity_change_pct is not None:
        change_score = _clip(55 + (liquidity_record.liquidity_change_pct / 100) * 50, 0, 100)
        signals.append(f"stored liquidity change {liquidity_record.liquidity_change_pct:.1f}%")

    lock_status = liquidity_record.lock_status if liquidity_record is not None else "UNKNOWN"
    confidence = 0.6
    if lock_status == "UNKNOWN":
        warnings.append("Liquidity lock/burn is UNKNOWN and is not treated as safe.")
        confidence = 0.45
        amount_score = min(amount_score, 85)
    parts = [amount_score] if change_score is None else [amount_score, change_score]
    return ComponentResult(
        LIQUIDITY,
        weight,
        round(sum(parts) / len(parts), 1),
        confidence,
        "MEASURED",
        "; ".join(signals),
        signals=signals,
        warnings=warnings,
    )


def score_security_adjustment(risk: TokenRiskAssessment | None, settings: Settings) -> ComponentResult:
    weight = SECURITY_COVERAGE_WEIGHT
    if risk is None:
        return ComponentResult(
            SECURITY,
            weight,
            None,
            0.0,
            "UNAVAILABLE",
            "No stored security assessment. Missing security is not evidence of safety and applies no bonus.",
            warnings=["Security data missing; confidence and coverage are reduced."],
        )

    classified = classify_measurements(
        settings,
        completeness=risk.completeness or 0.0,
        confidence=risk.confidence or 0.0,
        observed_risk=risk.observed_risk,
        observed_risk_band=risk.observed_risk_band,
        stored_risk_score=risk.risk_score,
        stored_risk_band=risk.risk_band,
    )
    unknown = _unknown_categories(risk.category_scores_json)
    warnings = []
    if unknown:
        warnings.append(
            "UNKNOWN security categories are excluded from confirmed penalties and are not treated as safe: "
            + ", ".join(unknown)
        )
    if classified.classification_reliable and classified.risk_score is not None:
        penalty = round(-settings.opportunity_max_security_penalty * (classified.risk_score / 100.0), 1)
        return ComponentResult(
            SECURITY,
            weight,
            penalty,
            round(_clip(risk.confidence, 0, 1), 4),
            "CONFIRMED",
            (
                f"Confirmed security classification {classified.risk_score}/100 "
                f"({classified.risk_band}) applied a {penalty} point penalty."
            ),
            signals=[f"confirmed security penalty {penalty}"],
            warnings=warnings,
        )

    observed = classified.observed_risk
    warnings.append(
        "Security assessment does not meet SECURITY_MIN_COMPLETENESS/"
        "SECURITY_MIN_CONFIDENCE, so no confirmed opportunity penalty was applied."
    )
    if observed is not None and observed >= settings.security_high_risk_threshold:
        warnings.append(
            f"Unconfirmed observed security risk {observed}/100 is a concern, not a confirmed classification."
        )
    return ComponentResult(
        SECURITY,
        weight,
        0.0,
        round(_clip((risk.confidence or 0) * 0.4, 0, 0.4), 4),
        "UNCONFIRMED",
        (
            f"Security status {classified.status}; observed risk "
            f"{observed if observed is not None else 'n/a'}. Penalty withheld."
        ),
        signals=["unconfirmed security observation; no confirmed penalty"],
        warnings=warnings,
    )


def _explanation(
    settings: Settings,
    *,
    status: str,
    label: str,
    opportunity_score: float | None,
    positive_score: float | None,
    adjustment: float,
    completeness: float,
    confidence: float,
    missing: list[str],
) -> str:
    return (
        f"{status}/{label}. Positive opportunity "
        f"{positive_score if positive_score is not None else 'n/a'} from available components only "
        f"(weights momentum {settings.opportunity_weight_momentum:.2f}, narrative "
        f"{settings.opportunity_weight_narrative:.2f}, discovery {settings.opportunity_weight_discovery:.2f}, "
        f"liquidity {settings.opportunity_weight_liquidity:.2f}); missing components "
        f"{', '.join(missing) or 'none'} were omitted, not zero-filled. "
        f"Security adjustment {adjustment:.1f} (confirmed penalty only). "
        f"Final opportunity {opportunity_score if opportunity_score is not None else 'n/a'}. "
        f"Completeness {completeness:.2f} (min {settings.opportunity_min_completeness:.2f}), "
        f"confidence {confidence:.2f} (min {settings.opportunity_min_confidence:.2f}). "
        "This is paper intelligence, not a prediction of profit."
    )


def _unknown_categories(raw: str | None) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    names = []
    for item in payload:
        if isinstance(item, dict) and str(item.get("status") or "").upper() == "UNKNOWN":
            names.append(str(item.get("name") or "unknown"))
    return names


def _txn_count(snapshot: MarketSnapshot) -> float | None:
    buys = snapshot.buys_24h
    sells = snapshot.sells_24h
    if buys is None and sells is None:
        return None
    return float((buys or 0) + (sells or 0))


def _safe_growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if previous <= 0 or current < 0:
        return None
    growth = (current - previous) / previous
    return _clip(growth, -1.0, 8.0)


def _map_growth(growth: float) -> float:
    if growth <= 0:
        return _clip(45 + growth * 45, 0, 45)
    return _clip(45 + growth * 30, 45, 100)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
