"""Point-in-time opportunity validation. Candidates exist before any future pairing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    LiquidityAnalysisRecord,
    MarketSnapshot,
    Narrative,
    PointInTimeCandidate,
    PointInTimeOutcome,
    PointInTimeRun,
    Token,
    TokenNarrativeMatch,
    TokenRiskAssessment,
)
from app.services.historical_evaluation import (
    EXCLUDED_GAP,
    EXCLUDED_INSUFFICIENT,
    HORIZON_HOUR,
    HORIZON_NEXT,
    SUPPORTED_HORIZONS,
    _next_observation,
    _one_hour_outcome,
    _path_extrema,
    net_return,
    pct_change,
    round_trip_cost,
    valuation_usd,
)
from app.services.market_data_quality import (
    EXTREME_REVERSAL,
    POSSIBLE_QUOTE_REGIME_CHANGE,
)
from app.services.opportunity_scoring import (
    HIGH_OPPORTUNITY,
    SCORED,
    assess_opportunity,
    observation_time,
    observations_as_of,
)
from app.services.quality_historical_evaluation import assess_interval_quality
from app.timeutil import as_utc, utc_now

G0 = "G0"
G1 = "G1"
G2 = "G2"
G3 = "G3"
GROUPS = (G0, G1, G2, G3)
OVERLAP_BASELINE = "baseline"
OVERLAP_1H = "1h"
OVERLAP_6H = "6h"
OVERLAP_POLICIES = (OVERLAP_BASELINE, OVERLAP_1H, OVERLAP_6H)
SPLIT_RESEARCH = "research"
SPLIT_PURGE = "purge"
SPLIT_HOLDOUT = "holdout"
MEASURED = "measured"
MISSING_OUTCOME = "missing_outcome"
INSUFFICIENT_HISTORY = "insufficient_history"
GAP = "gap"
MISSING_OUTCOME_VALUATION = "missing_outcome_valuation"
RESEARCH_HOURS = 48
PURGE_MINUTES = 90
E1 = "E1_INSUFFICIENT_HISTORY"
E2 = "E2_MISSING_VALUATION"
E3 = "E3_MISSING_PRICE"
E4 = "E4_PAIR_CHANGED"
E5 = "E5_PRIOR_QUOTE_REGIME"
MIN_SAMPLE = 30


def ordered_snapshots(snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
    return sorted(
        [item for item in snapshots if as_utc(item.recorded_at) is not None],
        key=lambda item: (as_utc(item.recorded_at), item.id or 0),
    )


def feature_history(ordered: list[MarketSnapshot], candidate: MarketSnapshot) -> list[MarketSnapshot]:
    """Candidate plus strictly earlier prints. Same-timestamp peers are excluded."""
    signal_at = as_utc(candidate.recorded_at)
    earlier = [item for item in ordered if as_utc(item.recorded_at) < signal_at]
    return earlier + [candidate]


def future_snapshots(ordered: list[MarketSnapshot], candidate: MarketSnapshot) -> list[MarketSnapshot]:
    """Prints strictly after T. Same-timestamp rows are never outcomes."""
    signal_at = as_utc(candidate.recorded_at)
    return [item for item in ordered if as_utc(item.recorded_at) > signal_at]


def assign_split(signal_at: datetime, t0: datetime, holdout_start: datetime) -> str:
    purge_start = holdout_start - timedelta(minutes=PURGE_MINUTES)
    if signal_at < t0:
        return SPLIT_RESEARCH
    if signal_at < purge_start:
        return SPLIT_RESEARCH
    if signal_at < holdout_start:
        return SPLIT_PURGE
    return SPLIT_HOLDOUT


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1.0 - weight) + ordered[high] * weight, 6)


def prior_regime_codes(history: list[MarketSnapshot], settings: Settings) -> set[str]:
    """Quote-regime flags already observable at T from feature history only."""
    codes: set[str] = set()
    if len(history) < 2:
        return codes
    for index in range(1, len(history)):
        prev = history[index - 1]
        nxt = history[index]
        previous = history[index - 2] if index >= 2 else None
        flags, _, _ = assess_interval_quality(prev, nxt, [prev, nxt], previous, settings)
        for item in flags:
            if item.code in {EXTREME_REVERSAL, POSSIBLE_QUOTE_REGIME_CHANGE}:
                codes.add(item.code)
    return codes


@dataclass
class CandidateRecord:
    token_id: int
    token_address: str
    symbol: str | None
    snapshot: MarketSnapshot
    signal_at: datetime
    split: str
    in_g1: bool = False
    in_g2: bool = False
    in_g3: bool = False
    entry_reasons: list[str] = field(default_factory=list)
    opportunity_status: str | None = None
    ranking_label: str | None = None
    opportunity_score: float | None = None
    completeness: float | None = None
    confidence: float | None = None
    overlap: dict = field(default_factory=dict)
    outcomes: dict = field(default_factory=dict)
    db_id: int | None = None


def classify_entry(
    candidate: MarketSnapshot,
    history: list[MarketSnapshot],
    settings: Settings,
    *,
    opportunity_status: str | None,
    ranking_label: str | None,
) -> tuple[bool, bool, bool, list[str]]:
    reasons: list[str] = []
    earlier = history[:-1] if history and history[-1] is candidate else [item for item in history if item is not candidate]
    if len(history) < 2 or not earlier:
        reasons.append(E1)
    if valuation_usd(candidate) is None:
        reasons.append(E2)
    if candidate.price_usd is None:
        reasons.append(E3)
    if earlier:
        previous = earlier[-1]
        if (previous.pair_address or "") != (candidate.pair_address or ""):
            reasons.append(E4)
    regime = prior_regime_codes(history, settings)
    if regime:
        reasons.append(E5)
    g1 = not reasons
    liq = candidate.liquidity_usd
    g2 = g1 and liq is not None and liq >= settings.discovery_min_liquidity_usd
    g3 = (
        g2
        and opportunity_status == SCORED
        and ranking_label == HIGH_OPPORTUNITY
    )
    return g1, g2, g3, reasons


def pair_outcome(
    candidate: MarketSnapshot,
    future: list[MarketSnapshot],
    *,
    horizon: str,
    max_gap_minutes: float,
    fee_bps: float,
    slippage_bps: float,
    settings: Settings,
) -> dict:
    timeline = [candidate, *future]
    max_gap = timedelta(minutes=max_gap_minutes)
    if not future:
        return {
            "horizon": horizon,
            "coverage_status": MISSING_OUTCOME,
            "outcome_snapshot_id": None,
            "outcome_at": None,
            "gross_return": None,
            "net_return": None,
            "mfe": None,
            "mae": None,
            "start_liquidity": candidate.liquidity_usd,
            "end_liquidity": None,
            "outcome_quality": [],
        }
    if horizon == HORIZON_NEXT:
        outcome, path, reason = _next_observation(timeline, 0, max_gap)
    else:
        outcome, path, reason = _one_hour_outcome(timeline, 0, max_gap, timedelta(hours=1))
    if reason == EXCLUDED_GAP:
        status = GAP
    elif reason == EXCLUDED_INSUFFICIENT or outcome is None:
        status = INSUFFICIENT_HISTORY if future else MISSING_OUTCOME
    else:
        status = MEASURED
    if outcome is not None and status == MEASURED:
        end_val = valuation_usd(outcome)
        start_val = valuation_usd(candidate)
        if end_val is None or start_val is None:
            status = MISSING_OUTCOME_VALUATION
            return {
                "horizon": horizon,
                "coverage_status": status,
                "outcome_snapshot_id": outcome.id,
                "outcome_at": as_utc(outcome.recorded_at),
                "gross_return": None,
                "net_return": None,
                "mfe": None,
                "mae": None,
                "start_liquidity": candidate.liquidity_usd,
                "end_liquidity": outcome.liquidity_usd,
                "outcome_quality": [],
            }
        gross = pct_change(start_val, end_val)
        if gross is None:
            status = MISSING_OUTCOME_VALUATION
            net = None
            mfe = mae = None
        else:
            cost = round_trip_cost(fee_bps, slippage_bps)
            net = net_return(gross, cost)
            visible = [item for item in path if as_utc(item.recorded_at) is not None]
            mfe, mae = _path_extrema(visible, start_val)
        flags, classification, _ = assess_interval_quality(
            candidate,
            outcome,
            path,
            None,
            settings,
        )
        return {
            "horizon": horizon,
            "coverage_status": status,
            "outcome_snapshot_id": outcome.id,
            "outcome_at": as_utc(outcome.recorded_at),
            "gross_return": round(gross, 6) if gross is not None else None,
            "net_return": round(net, 6) if net is not None else None,
            "mfe": round(mfe, 6) if mfe is not None else None,
            "mae": round(mae, 6) if mae is not None else None,
            "start_liquidity": candidate.liquidity_usd,
            "end_liquidity": outcome.liquidity_usd,
            "outcome_quality": {
                "classification": classification,
                "flags": [item.as_dict() for item in flags],
            },
        }
    return {
        "horizon": horizon,
        "coverage_status": status,
        "outcome_snapshot_id": None,
        "outcome_at": None,
        "gross_return": None,
        "net_return": None,
        "mfe": None,
        "mae": None,
        "start_liquidity": candidate.liquidity_usd,
        "end_liquidity": None,
        "outcome_quality": [],
    }


def apply_overlap(candidates: list[CandidateRecord]) -> None:
    for item in candidates:
        item.overlap = {group: {policy: 0 for policy in OVERLAP_POLICIES} for group in GROUPS}
        item.overlap[G0][OVERLAP_BASELINE] = 1
        if item.in_g1:
            item.overlap[G1][OVERLAP_BASELINE] = 1
        if item.in_g2:
            item.overlap[G2][OVERLAP_BASELINE] = 1
        if item.in_g3:
            item.overlap[G3][OVERLAP_BASELINE] = 1

    windows = {OVERLAP_1H: timedelta(hours=1), OVERLAP_6H: timedelta(hours=6)}

    def in_group(row: CandidateRecord, group: str) -> bool:
        if group == G0:
            return True
        if group == G1:
            return row.in_g1
        if group == G2:
            return row.in_g2
        return row.in_g3

    by_token: dict[int, list[CandidateRecord]] = {}
    for row in candidates:
        by_token.setdefault(row.token_id, []).append(row)
    for rows in by_token.values():
        rows.sort(key=lambda item: (item.signal_at, item.snapshot.id or 0))
        for group in GROUPS:
            members = [item for item in rows if in_group(item, group)]
            for policy, delta in windows.items():
                last_kept: datetime | None = None
                for item in members:
                    if last_kept is None or item.signal_at >= last_kept + delta:
                        item.overlap[group][policy] = 1
                        last_kept = item.signal_at
                    else:
                        item.overlap[group][policy] = 0


def _as_of_stamp(value: datetime | None, cutoff: datetime) -> bool:
    stamp = as_utc(value)
    return stamp is not None and stamp <= cutoff


def reconstruct_opportunity(
    settings: Settings,
    token: Token,
    history: list[MarketSnapshot],
    matches: list[tuple[TokenNarrativeMatch, Narrative | None]],
    risks: list[TokenRiskAssessment],
    liquidity_rows: list[LiquidityAnalysisRecord],
    signal_at: datetime,
):
    """As-of-T reconstruction. Does not read opportunity_assessments."""
    cutoff = signal_at
    visible_matches = []
    for match, narrative in matches:
        if narrative is None:
            continue
        detected = as_utc(narrative.last_detected_at) or as_utc(narrative.first_detected_at)
        match_at = as_utc(match.created_at)
        if detected is None or detected > cutoff:
            continue
        if match_at is not None and match_at > cutoff:
            continue
        visible_matches.append((match, narrative))
    risk = None
    eligible_risks = observations_as_of(risks, cutoff)
    if eligible_risks:
        risk = max(eligible_risks, key=lambda row: observation_time(row) or cutoff)
    liquidity = None
    for item in sorted(liquidity_rows, key=lambda row: as_utc(row.scanned_at) or cutoff, reverse=True):
        if _as_of_stamp(item.scanned_at, cutoff):
            liquidity = item
            break
    return assess_opportunity(settings, token, history, visible_matches, risk, liquidity, now=signal_at)


def _coverage_block(rows: list[dict]) -> dict:
    counts = {
        MEASURED: 0,
        MISSING_OUTCOME: 0,
        INSUFFICIENT_HISTORY: 0,
        GAP: 0,
        MISSING_OUTCOME_VALUATION: 0,
    }
    for item in rows:
        status = item.get("coverage_status")
        if status in counts:
            counts[status] += 1
        else:
            counts[MISSING_OUTCOME] += 1
    total = len(rows)
    measured = counts[MEASURED]
    missing = total - measured
    return {
        "candidates": total,
        "measured": measured,
        "missing": missing,
        "missing_breakdown": counts,
        "outcome_coverage": round(measured / total, 6) if total else None,
        "denominator_note": (
            "Performance rates use measured outcomes only. Missing outcomes are not zero returns. "
            "Candidate counts include rows without a future path."
        ),
    }


def _performance_block(measured: list[dict], cost: float) -> dict:
    gross_vals = [item["gross_return"] for item in measured if item.get("gross_return") is not None]
    net_vals = [item["net_return"] for item in measured if item.get("net_return") is not None]
    mfe_vals = [item["mfe"] for item in measured if item.get("mfe") is not None]
    mae_vals = [item["mae"] for item in measured if item.get("mae") is not None]
    sample = len(gross_vals)
    def pct(count: int) -> float | None:
        if sample <= 0:
            return None
        return round(count / sample, 6)

    gross_pos = sum(1 for value in gross_vals if value > 0)
    gross_neg = sum(1 for value in gross_vals if value < 0)
    zeros = sum(1 for value in gross_vals if value == 0)
    net_pos = sum(1 for value in gross_vals if value > cost)
    avg_gross = round(mean(gross_vals), 6) if gross_vals else None
    median_gross = round(median(gross_vals), 6) if gross_vals else None
    note = (
        "Historical DexScreener valuation-mark changes, not fills or executable trades. "
        f"Net-positive means gross > {cost:.4f}."
    )
    if (
        avg_gross is not None
        and median_gross is not None
        and gross_vals
        and max(abs(value) for value in gross_vals) >= 10
        and abs(avg_gross) > abs(median_gross) * 2 + 0.01
    ):
        note += f" Average gross ({avg_gross}) is dominated by outliers; median ({median_gross}) is more stable."
    both_liq = sum(
        1
        for item in measured
        if item.get("start_liquidity") is not None and item.get("end_liquidity") is not None
    )
    missing_liq = sample - both_liq
    quality_counts: dict[str, int] = {}
    for item in measured:
        payload = item.get("outcome_quality") or {}
        if isinstance(payload, dict):
            label = payload.get("classification")
            if label:
                quality_counts[label] = quality_counts.get(label, 0) + 1
    return {
        "measured_sample_size": sample,
        "performance_denominator": sample,
        "insufficient_data": sample < MIN_SAMPLE,
        "gross_positive": gross_pos,
        "gross_negative": gross_neg,
        "zero_return": zeros,
        "gross_positive_pct": pct(gross_pos),
        "gross_negative_pct": pct(gross_neg),
        "zero_return_pct": pct(zeros),
        "net_positive": net_pos,
        "net_positive_pct": pct(net_pos),
        "median_gross": median_gross,
        "median_net": round(median(net_vals), 6) if net_vals else None,
        "gross_p25": _percentile(gross_vals, 0.25),
        "gross_p75": _percentile(gross_vals, 0.75),
        "avg_gross": avg_gross,
        "avg_net": round(mean(net_vals), 6) if net_vals else None,
        "mfe": {
            "median": round(median(mfe_vals), 6) if mfe_vals else None,
            "max": round(max(mfe_vals), 6) if mfe_vals else None,
            "count": len(mfe_vals),
        },
        "mae": {
            "median": round(median(mae_vals), 6) if mae_vals else None,
            "min": round(min(mae_vals), 6) if mae_vals else None,
            "count": len(mae_vals),
        },
        "outcome_liquidity": {
            "measured": sample,
            "both_endpoints_non_null": both_liq,
            "missing_either": missing_liq,
            "missing_pct": round(missing_liq / sample, 6) if sample else None,
            "note": "Null liquidity is not treated as zero. Averages would use non-null pairs only.",
        },
        "outcome_quality_counts": quality_counts,
        "note": note,
    }


def _group_metrics(
    candidates: list[CandidateRecord],
    group: str,
    policy: str,
    horizon: str,
    cost: float,
    settings: Settings,
) -> dict:
    kept = [
        item
        for item in candidates
        if item.overlap.get(group, {}).get(policy)
    ]
    entry_missing_liq = sum(1 for item in kept if item.snapshot.liquidity_usd is None)
    outcomes = [item.outcomes.get(horizon, {}) for item in kept]
    coverage = _coverage_block(outcomes)
    measured = [item for item in outcomes if item.get("coverage_status") == MEASURED]
    performance = _performance_block(measured, cost)
    g1_n = sum(1 for item in candidates if item.in_g1 and item.overlap.get(G1, {}).get(OVERLAP_BASELINE))
    result = {
        "group": group,
        "overlap": policy,
        "horizon": horizon,
        "candidate_count": len(kept),
        "g1_missing_liquidity": entry_missing_liq if group == G1 else None,
        "g1_missing_liquidity_pct": (
            round(entry_missing_liq / len(kept), 6) if group == G1 and kept else None
        ),
        "coverage": coverage,
        "performance": performance,
        "experimental_liquidity_threshold_usd": settings.discovery_min_liquidity_usd,
        "liquidity_threshold_note": (
            f"${settings.discovery_min_liquidity_usd:,.0f} is an experimental G2 cut, not proof of tradeability."
        ),
    }
    if group == G2 and g1_n:
        baseline_g1 = sum(1 for item in candidates if item.overlap.get(G1, {}).get(policy))
        result["g2_coverage_vs_g1"] = round(len(kept) / baseline_g1, 6) if baseline_g1 else None
    return result


def build_summary(
    candidates: list[CandidateRecord],
    *,
    t0: datetime | None,
    holdout_start: datetime | None,
    settings: Settings,
    cost: float,
    snapshots_scanned: int,
) -> dict:
    research = [item for item in candidates if item.split == SPLIT_RESEARCH]
    purge = [item for item in candidates if item.split == SPLIT_PURGE]
    holdout = [item for item in candidates if item.split == SPLIT_HOLDOUT]
    research_tokens = {item.token_address for item in research}
    holdout_tokens = {item.token_address for item in holdout}
    nested = {
        "G0": sum(1 for item in candidates),
        "G1": sum(1 for item in candidates if item.in_g1),
        "G2": sum(1 for item in candidates if item.in_g2),
        "G3": sum(1 for item in candidates if item.in_g3),
        "nested_before_overlap": True,
    }
    g1 = [item for item in candidates if item.in_g1]
    g1_missing = sum(1 for item in g1 if item.snapshot.liquidity_usd is None)
    tables: dict = {}
    for split_name, rows in (
        ("all", candidates),
        (SPLIT_RESEARCH, research),
        (SPLIT_PURGE, purge),
        (SPLIT_HOLDOUT, holdout),
    ):
        tables[split_name] = {}
        for horizon in SUPPORTED_HORIZONS:
            tables[split_name][horizon] = {}
            for policy in OVERLAP_POLICIES:
                tables[split_name][horizon][policy] = {
                    group: _group_metrics(rows, group, policy, horizon, cost, settings)
                    for group in GROUPS
                }
                for group in GROUPS:
                    measured_n = tables[split_name][horizon][policy][group]["performance"]["measured_sample_size"]
                    if split_name == SPLIT_HOLDOUT and group in {G1, G2, G3} and measured_n < MIN_SAMPLE:
                        tables[split_name][horizon][policy][group]["insufficient_holdout"] = True
    purge_start = (holdout_start - timedelta(minutes=PURGE_MINUTES)) if holdout_start else None
    return {
        "status": "completed",
        "snapshots_scanned": snapshots_scanned,
        "candidate_count": len(candidates),
        "groups_before_overlap": nested,
        "g1_missing_liquidity": {
            "count": g1_missing,
            "pct": round(g1_missing / len(g1), 6) if g1 else None,
            "g1_count": len(g1),
            "g2_count": nested["G2"],
            "g2_coverage_vs_g1": round(nested["G2"] / len(g1), 6) if g1 else None,
            "note": "Null liquidity is not treated as zero. G1 allows missing liquidity; G2 does not.",
        },
        "g3_count": nested["G3"],
        "g3_insufficient": nested["G3"] < MIN_SAMPLE,
        "split": {
            "t0": t0.isoformat() if t0 else None,
            "holdout_start": holdout_start.isoformat() if holdout_start else None,
            "purge_minutes": PURGE_MINUTES,
            "research_range": (
                [t0.isoformat(), purge_start.isoformat()] if t0 and purge_start else None
            ),
            "purge_range": (
                [purge_start.isoformat(), holdout_start.isoformat()]
                if purge_start and holdout_start
                else None
            ),
            "holdout_range": (
                [holdout_start.isoformat(), None] if holdout_start else None
            ),
            "research_candidates": len(research),
            "purge_candidates": len(purge),
            "holdout_candidates": len(holdout),
            "tokens_in_research_and_holdout": len(research_tokens & holdout_tokens),
            "note": "Chronological temporal split, not an independent-token test.",
        },
        "metrics": tables,
        "thresholds": {
            "liquidity_usd": settings.discovery_min_liquidity_usd,
            "high_opportunity": settings.opportunity_high_threshold,
            "watchlist": settings.opportunity_watch_threshold,
            "min_completeness": settings.opportunity_min_completeness,
            "min_confidence": settings.opportunity_min_confidence,
            "round_trip_cost": cost,
            "max_gap_minutes": settings.evaluation_max_gap_minutes,
            "min_sample_for_claims": MIN_SAMPLE,
        },
        "look_ahead_safeguard": (
            "Candidates are every snapshot. Entry uses the candidate plus strictly earlier prints. "
            "Outcomes require recorded_at strictly after T. Same-timestamp peers are neither features "
            "nor outcomes. Opportunity_assessments and Phase 5 QUALITY_FILTERED are not entry groups."
        ),
        "paper_trading_only": True,
        "explanation": (
            "Point-in-time validation. Not paper trading, not fills, not a guarantee. "
            "G3 uses frozen HIGH_OPPORTUNITY/SCORED thresholds. Current data may yield zero G3 "
            "rows and insufficient holdout samples; thresholds are not relaxed."
        ),
    }


def run_point_in_time_evaluation(
    session: Session,
    settings: Settings | None = None,
    *,
    token_address: str | None = None,
    persist: bool = True,
) -> dict:
    settings = settings or get_settings()
    started = utc_now()
    cost = round_trip_cost(settings.evaluation_fee_bps, settings.evaluation_slippage_bps)
    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query).all())
    snaps = list(session.scalars(select(MarketSnapshot)).all())
    by_token: dict[int, list[MarketSnapshot]] = {}
    for snap in snaps:
        by_token.setdefault(snap.token_id, []).append(snap)

    match_rows = list(session.scalars(select(TokenNarrativeMatch)).all())
    narratives = {
        item.id: item
        for item in session.scalars(select(Narrative)).all()
    }
    matches_by_token: dict[int, list[tuple[TokenNarrativeMatch, Narrative | None]]] = {}
    for row in match_rows:
        matches_by_token.setdefault(row.token_id, []).append((row, narratives.get(row.narrative_id)))
    risks_by_token: dict[int, list[TokenRiskAssessment]] = {}
    for row in session.scalars(select(TokenRiskAssessment)).all():
        risks_by_token.setdefault(row.token_id, []).append(row)
    liq_by_token: dict[int, list[LiquidityAnalysisRecord]] = {}
    for row in session.scalars(select(LiquidityAnalysisRecord)).all():
        liq_by_token.setdefault(row.token_id, []).append(row)

    stamps = [as_utc(item.recorded_at) for item in snaps if as_utc(item.recorded_at)]
    t0 = min(stamps) if stamps else None
    holdout_start = t0 + timedelta(hours=RESEARCH_HOURS) if t0 else None

    candidates: list[CandidateRecord] = []
    for token in tokens:
        ordered = ordered_snapshots(by_token.get(token.id, []))
        for snap in ordered:
            signal_at = as_utc(snap.recorded_at)
            if signal_at is None:
                continue
            history = feature_history(ordered, snap)
            future = future_snapshots(ordered, snap)
            reconstructed = reconstruct_opportunity(
                settings,
                token,
                history,
                matches_by_token.get(token.id, []),
                risks_by_token.get(token.id, []),
                liq_by_token.get(token.id, []),
                signal_at,
            )
            g1, g2, g3, reasons = classify_entry(
                snap,
                history,
                settings,
                opportunity_status=reconstructed.status,
                ranking_label=reconstructed.ranking_label,
            )
            split = assign_split(signal_at, t0, holdout_start) if t0 and holdout_start else SPLIT_RESEARCH
            record = CandidateRecord(
                token_id=token.id,
                token_address=token.token_address,
                symbol=token.symbol,
                snapshot=snap,
                signal_at=signal_at,
                split=split,
                in_g1=g1,
                in_g2=g2,
                in_g3=g3,
                entry_reasons=reasons,
                opportunity_status=reconstructed.status,
                ranking_label=reconstructed.ranking_label,
                opportunity_score=reconstructed.opportunity_score,
                completeness=reconstructed.data_completeness,
                confidence=reconstructed.confidence,
            )
            record.outcomes[HORIZON_NEXT] = pair_outcome(
                snap,
                future,
                horizon=HORIZON_NEXT,
                max_gap_minutes=settings.evaluation_max_gap_minutes,
                fee_bps=settings.evaluation_fee_bps,
                slippage_bps=settings.evaluation_slippage_bps,
                settings=settings,
            )
            record.outcomes[HORIZON_HOUR] = pair_outcome(
                snap,
                future,
                horizon=HORIZON_HOUR,
                max_gap_minutes=settings.evaluation_max_gap_minutes,
                fee_bps=settings.evaluation_fee_bps,
                slippage_bps=settings.evaluation_slippage_bps,
                settings=settings,
            )
            candidates.append(record)

    apply_overlap(candidates)
    payload = build_summary(
        candidates,
        t0=t0,
        holdout_start=holdout_start,
        settings=settings,
        cost=cost,
        snapshots_scanned=len(snaps),
    )
    payload["started_at"] = started.isoformat()
    payload["finished_at"] = utc_now().isoformat()
    run_id = None
    if persist:
        run = PointInTimeRun(
            started_at=started,
            finished_at=utc_now(),
            status="completed",
            t0=t0,
            holdout_start=holdout_start,
            candidates_count=len(candidates),
            snapshots_scanned=len(snaps),
            summary_json=json.dumps(payload),
            explanation=payload["explanation"],
        )
        session.add(run)
        session.flush()
        run_id = run.id
        batch_c: list[PointInTimeCandidate] = []
        for item in candidates:
            row = PointInTimeCandidate(
                run_id=run.id,
                token_id=item.token_id,
                token_address=item.token_address,
                symbol=item.symbol,
                snapshot_id=item.snapshot.id,
                signal_at=item.signal_at,
                split=item.split,
                in_g0=1,
                in_g1=int(item.in_g1),
                in_g2=int(item.in_g2),
                in_g3=int(item.in_g3),
                entry_reasons_json=json.dumps(item.entry_reasons),
                liquidity_usd=item.snapshot.liquidity_usd,
                opportunity_status=item.opportunity_status,
                ranking_label=item.ranking_label,
                opportunity_score=item.opportunity_score,
                completeness=item.completeness,
                confidence=item.confidence,
                overlap_json=json.dumps(item.overlap),
            )
            batch_c.append(row)
            if len(batch_c) >= 400:
                session.add_all(batch_c)
                session.flush()
                batch_c = []
        if batch_c:
            session.add_all(batch_c)
            session.flush()
        stored = list(
            session.scalars(
                select(PointInTimeCandidate).where(PointInTimeCandidate.run_id == run.id)
            ).all()
        )
        by_key = {(row.token_id, row.snapshot_id): row for row in stored}
        batch_o: list[PointInTimeOutcome] = []
        for item in candidates:
            parent = by_key.get((item.token_id, item.snapshot.id))
            if parent is None:
                continue
            item.db_id = parent.id
            for horizon in SUPPORTED_HORIZONS:
                out = item.outcomes[horizon]
                batch_o.append(
                    PointInTimeOutcome(
                        run_id=run.id,
                        candidate_id=parent.id,
                        horizon=horizon,
                        coverage_status=out["coverage_status"],
                        outcome_snapshot_id=out.get("outcome_snapshot_id"),
                        outcome_at=out.get("outcome_at"),
                        gross_return=out.get("gross_return"),
                        net_return=out.get("net_return"),
                        mfe=out.get("mfe"),
                        mae=out.get("mae"),
                        start_liquidity=out.get("start_liquidity"),
                        end_liquidity=out.get("end_liquidity"),
                        outcome_quality_json=json.dumps(out.get("outcome_quality") or []),
                    )
                )
                if len(batch_o) >= 400:
                    session.add_all(batch_o)
                    session.flush()
                    batch_o = []
        if batch_o:
            session.add_all(batch_o)
        payload["run_id"] = run_id
        run.summary_json = json.dumps(payload)
        run.finished_at = utc_now()
        session.commit()
    payload["run_id"] = run_id
    return payload


def latest_point_in_time_run(session: Session) -> PointInTimeRun | None:
    return session.scalar(select(PointInTimeRun).order_by(PointInTimeRun.started_at.desc()).limit(1))


def list_point_in_time_runs(session: Session, limit: int = 25, offset: int = 0) -> tuple[list[PointInTimeRun], int]:
    rows = list(session.scalars(select(PointInTimeRun).order_by(PointInTimeRun.started_at.desc())).all())
    return rows[offset : offset + limit], len(rows)


def get_point_in_time_run(session: Session, run_id: int) -> PointInTimeRun | None:
    return session.get(PointInTimeRun, run_id)


def filter_summary(payload: dict, *, split: str | None, group: str | None, overlap: str | None) -> dict:
    metrics = payload.get("metrics") or {}
    if split:
        metrics = {split: metrics.get(split, {})}
    if group or overlap:
        trimmed: dict = {}
        for split_name, horizons in metrics.items():
            trimmed[split_name] = {}
            for horizon, policies in (horizons or {}).items():
                trimmed[split_name][horizon] = {}
                for policy, groups in (policies or {}).items():
                    if overlap and policy != overlap:
                        continue
                    if group:
                        trimmed[split_name][horizon][policy] = {group: groups.get(group, {})}
                    else:
                        trimmed[split_name][horizon][policy] = groups
        metrics = trimmed
    filtered = dict(payload)
    filtered["metrics"] = metrics
    return filtered
