"""Time-specific quality-aware historical evaluation. Does not rewrite evaluations 1–2."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    MarketSnapshot,
    QualityHistoricalEvaluation,
    QualityHistoricalOutcome,
    Token,
)
from app.services.historical_evaluation import (
    EXCLUDED_GAP,
    EXCLUDED_INSUFFICIENT,
    HORIZON_HOUR,
    HORIZON_NEXT,
    SUPPORTED_HORIZONS,
    _next_observation,
    _one_hour_outcome,
    _ordered,
    _path_extrema,
    _stamp,
    net_return,
    pct_change,
    round_trip_cost,
    valuation_usd,
)
from app.services.market_data_quality import (
    CLEAN,
    DUPLICATE_VALUATION,
    EXTREME_PRICE_MOVEMENT,
    EXTREME_REVERSAL,
    LIQUIDITY_DISLOCATION,
    MISSING_LIQUIDITY,
    MISSING_VALUATION,
    PAIR_CHANGED,
    POSSIBLE_QUOTE_REGIME_CHANGE,
    QualityFlag,
    STALE_SNAPSHOT,
    SUSPICIOUS,
    VALUATION_SOURCE_CHANGED,
    WARNING,
    classify_from_flags,
    valuation_basis,
)
from app.timeutil import utc_now

ALL_OBSERVED = "ALL_OBSERVED"
SAME_PAIR = "SAME_PAIR"
SAME_PAIR_AND_VALUATION = "SAME_PAIR_AND_VALUATION"
COMPLETE_LIQUIDITY = "COMPLETE_LIQUIDITY"
QUALITY_FILTERED = "QUALITY_FILTERED"
SUSPICIOUS_OR_EXCLUDED = "SUSPICIOUS_OR_EXCLUDED"
ALL_OBSERVED_NON_OVERLAPPING = "ALL_OBSERVED_NON_OVERLAPPING"
QUALITY_FILTERED_NON_OVERLAPPING = "QUALITY_FILTERED_NON_OVERLAPPING"

QUALITY_FILTER_FLAGS = {
    PAIR_CHANGED,
    VALUATION_SOURCE_CHANGED,
    MISSING_LIQUIDITY,
    MISSING_VALUATION,
    EXTREME_PRICE_MOVEMENT,
    EXTREME_REVERSAL,
    LIQUIDITY_DISLOCATION,
    POSSIBLE_QUOTE_REGIME_CHANGE,
    STALE_SNAPSHOT,
    DUPLICATE_VALUATION,
}


def _ratio(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0 or end < 0:
        return None
    return end / start


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


def _flag(
    code: str,
    detail: str,
    start: datetime | None,
    end: datetime | None,
) -> QualityFlag:
    return QualityFlag(code, detail, from_at=start, to_at=end)


def assess_interval_quality(
    signal: MarketSnapshot,
    outcome: MarketSnapshot,
    path: list[MarketSnapshot],
    previous: MarketSnapshot | None,
    settings: Settings,
) -> tuple[list[QualityFlag], str, list[str]]:
    """Flags use only snapshots at or before the outcome timestamp.

    `previous` is the snapshot immediately before the signal (also <= outcome).
    Future prints after outcome_at must not be supplied in `path`.
    """
    signal_at = _stamp(signal.recorded_at)
    outcome_at = _stamp(outcome.recorded_at)
    flags: list[QualityFlag] = []
    stale_after = timedelta(hours=settings.quality_stale_hours)
    multiple = settings.quality_extreme_move_multiple
    dislocation = settings.quality_liquidity_dislocation_ratio
    reversal_floor = 1.0 - settings.quality_extreme_reversal

    if (signal.pair_address or "") != (outcome.pair_address or ""):
        flags.append(
            _flag(
                PAIR_CHANGED,
                f"pair_address changed from {signal.pair_address!r} to {outcome.pair_address!r}.",
                signal_at,
                outcome_at,
            )
        )

    start_basis = valuation_basis(signal)
    end_basis = valuation_basis(outcome)
    if start_basis is None or end_basis is None:
        flags.append(
            _flag(
                MISSING_VALUATION,
                "Signal or outcome has no usable market cap or FDV.",
                signal_at,
                outcome_at,
            )
        )
    elif start_basis != end_basis:
        flags.append(
            _flag(
                VALUATION_SOURCE_CHANGED,
                f"Valuation source changed from {start_basis} to {end_basis}.",
                signal_at,
                outcome_at,
            )
        )

    if signal.liquidity_usd is None or outcome.liquidity_usd is None:
        flags.append(
            _flag(
                MISSING_LIQUIDITY,
                "Liquidity is null at the signal and/or outcome. Not treated as zero.",
                signal_at,
                outcome_at,
            )
        )

    start_val = valuation_usd(signal)
    end_val = valuation_usd(outcome)
    if start_val is not None and end_val is not None and start_val == end_val:
        same_price = signal.price_usd is None or outcome.price_usd is None or signal.price_usd == outcome.price_usd
        if same_price:
            flags.append(
                _flag(
                    DUPLICATE_VALUATION,
                    f"Repeated valuation {start_val} between signal and outcome.",
                    signal_at,
                    outcome_at,
                )
            )

    extreme_highs: list[tuple[datetime, float]] = []

    def _move_flags(left: MarketSnapshot, right: MarketSnapshot) -> None:
        left_at = _stamp(left.recorded_at)
        right_at = _stamp(right.recorded_at)
        if left_at and right_at and right_at - left_at > stale_after:
            flags.append(
                _flag(
                    STALE_SNAPSHOT,
                    f"Gap of {(right_at - left_at).total_seconds() / 3600:.2f} hours exceeds stale threshold.",
                    left_at,
                    right_at,
                )
            )
        left_val = valuation_usd(left)
        right_val = valuation_usd(right)
        val_ratio = _ratio(left_val, right_val)
        px_ratio = _ratio(left.price_usd, right.price_usd)
        extreme = False
        if val_ratio is not None and val_ratio > multiple:
            extreme = True
            flags.append(
                _flag(
                    EXTREME_PRICE_MOVEMENT,
                    f"Valuation ratio {val_ratio:.4f} exceeds {multiple}x.",
                    left_at,
                    right_at,
                )
            )
        if px_ratio is not None and px_ratio > multiple:
            extreme = True
            flags.append(
                _flag(
                    EXTREME_PRICE_MOVEMENT,
                    f"price_usd ratio {px_ratio:.4f} exceeds {multiple}x.",
                    left_at,
                    right_at,
                )
            )
        if extreme and right_val is not None and right_at is not None:
            extreme_highs.append((right_at, right_val))
        liq_ratio = _ratio(left.liquidity_usd, right.liquidity_usd)
        if liq_ratio is not None and (
            liq_ratio > dislocation or (liq_ratio > 0 and 1 / liq_ratio > dislocation)
        ):
            flags.append(
                _flag(
                    LIQUIDITY_DISLOCATION,
                    f"Liquidity ratio {liq_ratio:.4f} exceeds {dislocation}x. Both endpoints were non-null.",
                    left_at,
                    right_at,
                )
            )
        pair_same = (left.pair_address or "") == (right.pair_address or "")
        if extreme and pair_same and liq_ratio is not None and liq_ratio > dislocation:
            flags.append(
                _flag(
                    POSSIBLE_QUOTE_REGIME_CHANGE,
                    "Extreme move with unchanged pair and a large liquidity jump. Not proof of an executable trade.",
                    left_at,
                    right_at,
                )
            )

    visible = [item for item in path if _stamp(item.recorded_at) is not None]
    if previous is not None and _stamp(previous.recorded_at) is not None:
        prev_at = _stamp(previous.recorded_at)
        if outcome_at is None or prev_at <= outcome_at:
            _move_flags(previous, signal)

    for left, right in zip(visible, visible[1:]):
        _move_flags(left, right)

    for high_at, high_val in extreme_highs:
        for snap in visible:
            stamp = _stamp(snap.recorded_at)
            if stamp is None or stamp <= high_at:
                continue
            if outcome_at is not None and stamp > outcome_at:
                continue
            later = valuation_usd(snap)
            if later is None or high_val <= 0:
                continue
            if later / high_val <= reversal_floor:
                flags.append(
                    _flag(
                        EXTREME_REVERSAL,
                        (
                            f"After a >{multiple}x increase, valuation fell "
                            f"{1 - later / high_val:.2%} from {high_val} to {later} "
                            "by this outcome time."
                        ),
                        high_at,
                        stamp,
                    )
                )
                flags.append(
                    _flag(
                        POSSIBLE_QUOTE_REGIME_CHANGE,
                        "Extreme increase followed by a >=90% reversal already visible at outcome time.",
                        high_at,
                        stamp,
                    )
                )
                break

    unique: list[QualityFlag] = []
    seen: set[tuple] = set()
    for item in flags:
        key = (item.code, item.from_at, item.to_at, item.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    classification = classify_from_flags(unique)
    exclusion = sorted({item.code for item in unique if item.code in QUALITY_FILTER_FLAGS})
    return unique, classification, exclusion


@dataclass
class QualityOutcome:
    token_id: int
    token_address: str
    symbol: str | None
    signal_snapshot_id: int | None
    outcome_snapshot_id: int | None
    signal_at: datetime
    outcome_at: datetime
    start_valuation: float | None
    end_valuation: float | None
    start_valuation_source: str | None
    end_valuation_source: str | None
    start_price: float | None
    end_price: float | None
    start_liquidity: float | None
    end_liquidity: float | None
    start_pair_address: str | None
    end_pair_address: str | None
    gross_return: float | None
    net_return: float | None
    mfe: float | None
    mae: float | None
    horizon: str
    flags: list[QualityFlag] = field(default_factory=list)
    classification: str = CLEAN
    exclusion_reasons: list[str] = field(default_factory=list)
    in_same_pair: bool = False
    in_same_pair_and_valuation: bool = False
    in_complete_liquidity: bool = False
    in_quality_filtered: bool = False
    in_suspicious_or_excluded: bool = False
    overlaps_prior_window: bool = False

    def flag_codes(self) -> list[str]:
        return [item.code for item in self.flags]


def mark_overlapping_windows(outcomes: list[QualityOutcome], horizon: str) -> None:
    """Greedy non-overlap: keep earliest signal per token; later overlapping windows are marked, not dropped."""
    if horizon != HORIZON_HOUR:
        return
    by_token: dict[int, list[QualityOutcome]] = {}
    for item in outcomes:
        by_token.setdefault(item.token_id, []).append(item)
    for rows in by_token.values():
        rows.sort(key=lambda item: (item.signal_at, item.outcome_at, item.signal_snapshot_id or 0))
        last_end: datetime | None = None
        for item in rows:
            if last_end is not None and item.signal_at < last_end:
                item.overlaps_prior_window = True
            else:
                item.overlaps_prior_window = False
                last_end = item.outcome_at


def _cohort_stats(rows: list[QualityOutcome], cost: float, *, denominator_note: str) -> dict:
    sample = len(rows)
    gross_vals = [item.gross_return for item in rows if item.gross_return is not None]
    net_vals = [item.net_return for item in rows if item.net_return is not None]
    mfe_vals = [item.mfe for item in rows if item.mfe is not None]
    mae_vals = [item.mae for item in rows if item.mae is not None]
    gross_pos = sum(1 for value in gross_vals if value > 0)
    gross_neg = sum(1 for value in gross_vals if value < 0)
    gross_zero = sum(1 for value in gross_vals if value == 0)
    net_pos = sum(1 for value in gross_vals if value > cost)
    both_liq = sum(1 for item in rows if item.start_liquidity is not None and item.end_liquidity is not None)
    missing_liq = sample - both_liq

    def pct(count: int) -> float | None:
        if sample <= 0:
            return None
        return round(count / sample, 6)

    avg_gross = round(mean(gross_vals), 6) if gross_vals else None
    median_gross = round(median(gross_vals), 6) if gross_vals else None
    outlier_note = (
        "These are DexScreener valuation changes, not executable trading returns. "
        "Hypothetical net return subtracts the configured round-trip cost; net-positive means gross > "
        f"{cost:.4f}."
    )
    if (
        avg_gross is not None
        and median_gross is not None
        and gross_vals
        and max(abs(value) for value in gross_vals) >= 10
        and abs(avg_gross) > abs(median_gross) * 2 + 0.01
    ):
        outlier_note += (
            f" Average gross ({avg_gross}) is dominated by extreme outliers; median ({median_gross}) "
            "is the more stable location statistic."
        )

    flag_counts: dict[str, int] = {}
    for item in rows:
        for code in set(item.flag_codes()):
            flag_counts[code] = flag_counts.get(code, 0) + 1

    return {
        "sample_size": sample,
        "denominator": sample,
        "denominator_note": denominator_note,
        "gross_positive": gross_pos,
        "gross_negative": gross_neg,
        "zero_return": gross_zero,
        "gross_positive_pct": pct(gross_pos),
        "gross_negative_pct": pct(gross_neg),
        "zero_return_pct": pct(gross_zero),
        "net_positive": net_pos,
        "net_positive_pct": pct(net_pos),
        "gross_return": {
            "avg": avg_gross,
            "median": median_gross,
            "p25": _percentile(gross_vals, 0.25),
            "p75": _percentile(gross_vals, 0.75),
            "min": round(min(gross_vals), 6) if gross_vals else None,
            "max": round(max(gross_vals), 6) if gross_vals else None,
            "count": len(gross_vals),
        },
        "net_return": {
            "avg": round(mean(net_vals), 6) if net_vals else None,
            "median": round(median(net_vals), 6) if net_vals else None,
            "p25": _percentile(net_vals, 0.25),
            "p75": _percentile(net_vals, 0.75),
            "min": round(min(net_vals), 6) if net_vals else None,
            "max": round(max(net_vals), 6) if net_vals else None,
            "count": len(net_vals),
        },
        "max_favorable_excursion": {
            "avg": round(mean(mfe_vals), 6) if mfe_vals else None,
            "median": round(median(mfe_vals), 6) if mfe_vals else None,
            "max": round(max(mfe_vals), 6) if mfe_vals else None,
            "count": len(mfe_vals),
        },
        "max_adverse_excursion": {
            "avg": round(mean(mae_vals), 6) if mae_vals else None,
            "median": round(median(mae_vals), 6) if mae_vals else None,
            "min": round(min(mae_vals), 6) if mae_vals else None,
            "count": len(mae_vals),
        },
        "liquidity": {
            "total_observations": sample,
            "both_endpoints_non_null": both_liq,
            "missing_either_endpoint": missing_liq,
            "missing_pct": pct(missing_liq),
            "note": "Missing liquidity is not treated as zero. Denominator is this cohort's sample_size.",
        },
        "flag_outcome_counts": flag_counts,
        "note": outlier_note,
    }


def _exclusion_breakdown(rows: list[QualityOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rows:
        reasons = item.exclusion_reasons or ["UNCLASSIFIED"]
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def overlapping_analysis(outcomes: list[QualityOutcome], cost: float, horizon: str) -> dict:
    total = len(outcomes)
    overlapping = sum(1 for item in outcomes if item.overlaps_prior_window)
    kept = [item for item in outcomes if not item.overlaps_prior_window]
    return {
        "horizon": horizon,
        "methodology": (
            "For one_hour, outcomes are sorted by signal time within each token. "
            "The earliest window is kept. A later window is overlapping when its signal_at is strictly "
            "before the previous kept window's outcome_at. Overlapping rows stay in ALL_OBSERVED; "
            "they are omitted only from NON_OVERLAPPING diagnostic cohorts."
        ),
        "applies": horizon == HORIZON_HOUR,
        "original_outcome_count": total,
        "overlapping_outcome_count": overlapping if horizon == HORIZON_HOUR else 0,
        "non_overlapping_outcome_count": len(kept) if horizon == HORIZON_HOUR else total,
        "before_deduplication": _cohort_stats(
            outcomes,
            cost,
            denominator_note="ALL_OBSERVED including overlapping one-hour windows.",
        ),
        "after_deduplication": _cohort_stats(
            kept if horizon == HORIZON_HOUR else outcomes,
            cost,
            denominator_note="Non-overlapping windows only (greedy earliest-signal keep).",
        ),
    }


def build_cohorts(outcomes: list[QualityOutcome], cost: float, horizon: str) -> dict:
    filtered = [item for item in outcomes if item.in_quality_filtered]
    excluded = [item for item in outcomes if item.in_suspicious_or_excluded]
    non_overlap = [item for item in outcomes if not item.overlaps_prior_window]
    filtered_non_overlap = [item for item in filtered if not item.overlaps_prior_window]
    cohorts = {
        ALL_OBSERVED: _cohort_stats(
            outcomes,
            cost,
            denominator_note="All measured valuation outcomes (same pairing rules as the original evaluator).",
        ),
        SAME_PAIR: _cohort_stats(
            [item for item in outcomes if item.in_same_pair],
            cost,
            denominator_note="Measured outcomes whose signal and outcome pair_address are equal.",
        ),
        SAME_PAIR_AND_VALUATION: _cohort_stats(
            [item for item in outcomes if item.in_same_pair_and_valuation],
            cost,
            denominator_note="Measured outcomes with the same pair_address and the same valuation source.",
        ),
        COMPLETE_LIQUIDITY: _cohort_stats(
            [item for item in outcomes if item.in_complete_liquidity],
            cost,
            denominator_note="Measured outcomes with non-null liquidity at both endpoints.",
        ),
        QUALITY_FILTERED: _cohort_stats(
            filtered,
            cost,
            denominator_note="Measured outcomes with no time-specific quality-filter flags.",
        ),
        SUSPICIOUS_OR_EXCLUDED: _cohort_stats(
            excluded,
            cost,
            denominator_note="Measured outcomes excluded from QUALITY_FILTERED. Not deleted from ALL_OBSERVED.",
        ),
    }
    if horizon == HORIZON_HOUR:
        cohorts[ALL_OBSERVED_NON_OVERLAPPING] = _cohort_stats(
            non_overlap,
            cost,
            denominator_note="ALL_OBSERVED after greedy non-overlap. Overlaps remain in ALL_OBSERVED.",
        )
        cohorts[QUALITY_FILTERED_NON_OVERLAPPING] = _cohort_stats(
            filtered_non_overlap,
            cost,
            denominator_note="QUALITY_FILTERED after greedy non-overlap.",
        )
    for name, block in cohorts.items():
        block["cohort"] = name
        if name == SUSPICIOUS_OR_EXCLUDED:
            block["exclusion_reason_counts"] = _exclusion_breakdown(excluded)
            block["exclusion_reason_denominator"] = (
                "Counts are outcomes that carry each reason; one outcome may have several reasons."
            )
    return cohorts


def evaluate_token_quality_outcomes(
    token: Token,
    snapshots: list[MarketSnapshot],
    *,
    horizon: str,
    fee_bps: float,
    slippage_bps: float,
    max_gap_minutes: float,
    settings: Settings,
) -> tuple[list[QualityOutcome], dict[str, int]]:
    coverage = {
        "tokens_considered": 1,
        "signals_considered": 0,
        "outcomes_measured": 0,
        "excluded_gap": 0,
        "excluded_missing_valuation": 0,
        "excluded_missing_outcome": 0,
        "excluded_insufficient_history": 0,
    }
    ordered = _ordered(snapshots)
    if len(ordered) < 2:
        coverage["signals_considered"] = len(ordered)
        coverage["excluded_insufficient_history"] = len(ordered)
        return [], coverage

    max_gap = timedelta(minutes=max_gap_minutes)
    hour = timedelta(hours=1)
    cost = round_trip_cost(fee_bps, slippage_bps)
    rows: list[QualityOutcome] = []

    for index, signal in enumerate(ordered):
        coverage["signals_considered"] += 1
        start_value = valuation_usd(signal)
        if start_value is None:
            coverage["excluded_missing_valuation"] += 1
            continue
        if horizon == HORIZON_NEXT:
            outcome_snap, path, reason = _next_observation(ordered, index, max_gap)
        elif horizon == HORIZON_HOUR:
            outcome_snap, path, reason = _one_hour_outcome(ordered, index, max_gap, hour)
        else:
            raise ValueError(f"Unsupported horizon: {horizon}")
        if reason == EXCLUDED_GAP:
            coverage["excluded_gap"] += 1
            continue
        if reason == EXCLUDED_INSUFFICIENT or outcome_snap is None:
            coverage["excluded_insufficient_history"] += 1
            continue
        end_value = valuation_usd(outcome_snap)
        if end_value is None:
            coverage["excluded_missing_outcome"] += 1
            continue
        gross = pct_change(start_value, end_value)
        if gross is None:
            coverage["excluded_missing_outcome"] += 1
            continue

        signal_at = _stamp(signal.recorded_at)
        outcome_at = _stamp(outcome_snap.recorded_at)
        assert signal_at is not None and outcome_at is not None
        visible_path = [item for item in path if (_stamp(item.recorded_at) or outcome_at) <= outcome_at]
        previous = ordered[index - 1] if index > 0 else None
        flags, classification, exclusion = assess_interval_quality(
            signal,
            outcome_snap,
            visible_path,
            previous,
            settings,
        )
        same_pair = (signal.pair_address or "") == (outcome_snap.pair_address or "")
        same_source = (
            valuation_basis(signal) is not None
            and valuation_basis(signal) == valuation_basis(outcome_snap)
        )
        complete_liq = signal.liquidity_usd is not None and outcome_snap.liquidity_usd is not None
        quality_ok = classification == CLEAN and not exclusion
        mfe, mae = _path_extrema(visible_path, start_value)
        net = net_return(gross, cost)
        rows.append(
            QualityOutcome(
                token_id=token.id or 0,
                token_address=token.token_address,
                symbol=token.symbol,
                signal_snapshot_id=signal.id,
                outcome_snapshot_id=outcome_snap.id,
                signal_at=signal_at,
                outcome_at=outcome_at,
                start_valuation=start_value,
                end_valuation=end_value,
                start_valuation_source=valuation_basis(signal),
                end_valuation_source=valuation_basis(outcome_snap),
                start_price=signal.price_usd,
                end_price=outcome_snap.price_usd,
                start_liquidity=signal.liquidity_usd,
                end_liquidity=outcome_snap.liquidity_usd,
                start_pair_address=signal.pair_address,
                end_pair_address=outcome_snap.pair_address,
                gross_return=round(gross, 6),
                net_return=round(net, 6) if net is not None else None,
                mfe=round(mfe, 6) if mfe is not None else None,
                mae=round(mae, 6) if mae is not None else None,
                horizon=horizon,
                flags=flags,
                classification=classification,
                exclusion_reasons=exclusion,
                in_same_pair=same_pair,
                in_same_pair_and_valuation=same_pair and same_source,
                in_complete_liquidity=complete_liq,
                in_quality_filtered=quality_ok,
                in_suspicious_or_excluded=not quality_ok,
            )
        )
        coverage["outcomes_measured"] += 1
    return rows, coverage


def _merge_coverage(total: dict[str, int], part: dict[str, int]) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + value


def run_quality_historical_evaluation(
    session: Session,
    settings: Settings | None = None,
    *,
    horizon: str = HORIZON_NEXT,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    token_address: str | None = None,
    persist: bool = True,
) -> dict:
    settings = settings or get_settings()
    if horizon not in SUPPORTED_HORIZONS:
        raise ValueError(f"Unsupported horizon {horizon}. Use {SUPPORTED_HORIZONS}.")
    fee = settings.evaluation_fee_bps if fee_bps is None else fee_bps
    slip = settings.evaluation_slippage_bps if slippage_bps is None else slippage_bps
    if fee < 0 or slip < 0:
        raise ValueError("Fee and slippage basis points must be >= 0")

    started = utc_now()
    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query).all())
    snapshots = list(session.scalars(select(MarketSnapshot)).all())
    by_token: dict[int, list[MarketSnapshot]] = {}
    for snap in snapshots:
        by_token.setdefault(snap.token_id, []).append(snap)

    all_outcomes: list[QualityOutcome] = []
    coverage = {
        "tokens_considered": 0,
        "signals_considered": 0,
        "outcomes_measured": 0,
        "excluded_gap": 0,
        "excluded_missing_valuation": 0,
        "excluded_missing_outcome": 0,
        "excluded_insufficient_history": 0,
    }
    for token in tokens:
        rows, part = evaluate_token_quality_outcomes(
            token,
            by_token.get(token.id, []),
            horizon=horizon,
            fee_bps=fee,
            slippage_bps=slip,
            max_gap_minutes=settings.evaluation_max_gap_minutes,
            settings=settings,
        )
        _merge_coverage(coverage, part)
        all_outcomes.extend(rows)

    mark_overlapping_windows(all_outcomes, horizon)
    cost = round_trip_cost(fee, slip)
    cohorts = build_cohorts(all_outcomes, cost, horizon)
    overlap = overlapping_analysis(all_outcomes, cost, horizon)
    finished = utc_now()
    explanation = (
        f"Quality-aware historical evaluation for horizon={horizon}. "
        "ALL_OBSERVED uses the original pairing rules. Quality flags use only snapshots at or before "
        "each outcome timestamp. Token-level quality scan classifications are not applied retroactively. "
        "CLEAN/QUALITY_FILTERED is not a safety or profit rating. Returns are DexScreener valuation "
        "changes, not fills. Missing liquidity stays null. Overlapping one-hour windows remain in "
        "ALL_OBSERVED. Paper trading only."
    )
    payload = {
        "status": "completed",
        "horizon": horizon,
        "fee_bps": fee,
        "slippage_bps": slip,
        "round_trip_cost": cost,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "tokens_considered": coverage["tokens_considered"],
        "signals_considered": coverage["signals_considered"],
        "outcomes_measured": coverage["outcomes_measured"],
        "snapshots_scanned": len(snapshots),
        "coverage": coverage,
        "cohorts": cohorts,
        "overlapping_windows": overlap,
        "thresholds": {
            "extreme_move_multiple": settings.quality_extreme_move_multiple,
            "extreme_reversal": settings.quality_extreme_reversal,
            "stale_hours": settings.quality_stale_hours,
            "liquidity_dislocation_ratio": settings.quality_liquidity_dislocation_ratio,
            "max_gap_minutes": settings.evaluation_max_gap_minutes,
        },
        "explanation": explanation,
        "paper_trading_only": True,
        "look_ahead_safeguard": (
            "Each outcome is scored from its signal, its path through the outcome print, and at most "
            "the immediately previous snapshot. Snapshots after outcome_at are ignored. "
            "data_quality_findings token labels are not used."
        ),
    }
    evaluation_id = None
    if persist:
        row = QualityHistoricalEvaluation(
            started_at=started,
            finished_at=finished,
            status="completed",
            horizon=horizon,
            fee_bps=fee,
            slippage_bps=slip,
            tokens_considered=coverage["tokens_considered"],
            signals_considered=coverage["signals_considered"],
            outcomes_measured=coverage["outcomes_measured"],
            snapshots_scanned=len(snapshots),
            summary_json=json.dumps(payload),
            explanation=explanation,
        )
        session.add(row)
        session.flush()
        evaluation_id = row.id
        batch: list[QualityHistoricalOutcome] = []
        for item in all_outcomes:
            batch.append(
                QualityHistoricalOutcome(
                    evaluation_id=row.id,
                    token_id=item.token_id,
                    token_address=item.token_address,
                    symbol=item.symbol,
                    signal_snapshot_id=item.signal_snapshot_id,
                    outcome_snapshot_id=item.outcome_snapshot_id,
                    signal_at=item.signal_at,
                    outcome_at=item.outcome_at,
                    start_valuation=item.start_valuation,
                    end_valuation=item.end_valuation,
                    start_valuation_source=item.start_valuation_source,
                    end_valuation_source=item.end_valuation_source,
                    start_price=item.start_price,
                    end_price=item.end_price,
                    start_liquidity=item.start_liquidity,
                    end_liquidity=item.end_liquidity,
                    start_pair_address=item.start_pair_address,
                    end_pair_address=item.end_pair_address,
                    gross_return=item.gross_return,
                    net_return=item.net_return,
                    mfe=item.mfe,
                    mae=item.mae,
                    horizon=horizon,
                    quality_flags_json=json.dumps([flag.as_dict() for flag in item.flags]),
                    quality_classification=item.classification,
                    exclusion_reasons_json=json.dumps(item.exclusion_reasons),
                    in_same_pair=int(item.in_same_pair),
                    in_same_pair_and_valuation=int(item.in_same_pair_and_valuation),
                    in_complete_liquidity=int(item.in_complete_liquidity),
                    in_quality_filtered=int(item.in_quality_filtered),
                    in_suspicious_or_excluded=int(item.in_suspicious_or_excluded),
                    overlaps_prior_window=int(item.overlaps_prior_window),
                )
            )
            if len(batch) >= 500:
                session.add_all(batch)
                session.flush()
                batch = []
        if batch:
            session.add_all(batch)
        payload["evaluation_id"] = evaluation_id
        row.summary_json = json.dumps(payload)
        session.commit()
    payload["evaluation_id"] = evaluation_id
    payload["outcomes"] = [
        {
            "token_address": item.token_address,
            "symbol": item.symbol,
            "signal_at": item.signal_at.isoformat(),
            "outcome_at": item.outcome_at.isoformat(),
            "gross_return": item.gross_return,
            "net_return": item.net_return,
            "classification": item.classification,
            "flags": [flag.as_dict() for flag in item.flags],
            "exclusion_reasons": item.exclusion_reasons,
            "in_quality_filtered": item.in_quality_filtered,
            "overlaps_prior_window": item.overlaps_prior_window,
        }
        for item in all_outcomes
    ]
    return payload


def latest_quality_evaluation(session: Session) -> QualityHistoricalEvaluation | None:
    return session.scalar(
        select(QualityHistoricalEvaluation).order_by(QualityHistoricalEvaluation.started_at.desc()).limit(1)
    )


def list_quality_evaluations(
    session: Session, limit: int = 25, offset: int = 0
) -> tuple[list[QualityHistoricalEvaluation], int]:
    rows = list(
        session.scalars(
            select(QualityHistoricalEvaluation).order_by(QualityHistoricalEvaluation.started_at.desc())
        ).all()
    )
    return rows[offset : offset + limit], len(rows)


def get_quality_evaluation(session: Session, evaluation_id: int) -> QualityHistoricalEvaluation | None:
    return session.get(QualityHistoricalEvaluation, evaluation_id)
