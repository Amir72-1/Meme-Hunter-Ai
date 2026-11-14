"""Market-only historical evaluation from stored snapshots. No live APIs or trades."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import HistoricalEvaluation, MarketSnapshot, Token
from app.timeutil import as_utc, utc_now

HORIZON_NEXT = "next_observation"
HORIZON_HOUR = "one_hour"
SUPPORTED_HORIZONS = (HORIZON_NEXT, HORIZON_HOUR)

PRIOR_UP = "PRIOR_UP"
PRIOR_DOWN = "PRIOR_DOWN"
PRIOR_UNCHANGED = "PRIOR_UNCHANGED"
NO_PRIOR = "NO_PRIOR"

EXCLUDED_GAP = "gap"
EXCLUDED_MISSING_VALUATION = "missing_valuation"
EXCLUDED_MISSING_OUTCOME = "missing_outcome"
EXCLUDED_INSUFFICIENT = "insufficient_history"


def _stamp(value: datetime | None) -> datetime | None:
    return as_utc(value)


def valuation_usd(snapshot: MarketSnapshot) -> float | None:
    """Market cap, else FDV. Missing values stay missing; never coerced to zero."""

    if snapshot.market_cap is not None and snapshot.market_cap > 0:
        return float(snapshot.market_cap)
    if snapshot.fdv is not None and snapshot.fdv > 0:
        return float(snapshot.fdv)
    return None


def pct_change(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return (end - start) / start


def round_trip_cost(fee_bps: float, slippage_bps: float) -> float:
    return 2.0 * ((fee_bps + slippage_bps) / 10_000.0)


def net_return(gross: float | None, cost: float) -> float | None:
    if gross is None:
        return None
    return gross - cost


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "median": None, "min": None, "max": None, "count": 0}
    return {
        "avg": round(mean(values), 6),
        "median": round(median(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "count": len(values),
    }


def _win_rate(positive: int, negative: int, zero: int) -> float | None:
    measured = positive + negative + zero
    if measured <= 0:
        return None
    return round(positive / measured, 6)


@dataclass
class PathOutcome:
    token_address: str
    signal_at: datetime
    outcome_at: datetime
    category: str
    elapsed_minutes: float
    gross_return: float | None
    net_return: float | None
    volume_change: float | None
    liquidity_change: float | None
    mfe: float | None
    mae: float | None
    positive: bool | None


@dataclass
class Coverage:
    tokens_considered: int = 0
    signals_considered: int = 0
    outcomes_measured: int = 0
    excluded_gap: int = 0
    excluded_missing_valuation: int = 0
    excluded_missing_outcome: int = 0
    excluded_insufficient_history: int = 0
    valuation_changes_measured: int = 0
    volume_changes_measured: int = 0
    liquidity_changes_measured: int = 0
    liquidity_missing_at_signal: int = 0
    mfe_mae_measured: int = 0

    def as_dict(self) -> dict[str, float | int | None]:
        attempted = self.signals_considered
        measured = self.outcomes_measured
        return {
            "tokens_considered": self.tokens_considered,
            "signals_considered": attempted,
            "outcomes_measured": measured,
            "outcome_coverage": round(measured / attempted, 6) if attempted else None,
            "excluded_gap": self.excluded_gap,
            "excluded_missing_valuation": self.excluded_missing_valuation,
            "excluded_missing_outcome": self.excluded_missing_outcome,
            "excluded_insufficient_history": self.excluded_insufficient_history,
            "valuation_changes_measured": self.valuation_changes_measured,
            "volume_changes_measured": self.volume_changes_measured,
            "liquidity_changes_measured": self.liquidity_changes_measured,
            "liquidity_missing_at_signal": self.liquidity_missing_at_signal,
            "liquidity_missing_rate_at_signal": (
                round(self.liquidity_missing_at_signal / attempted, 6) if attempted else None
            ),
            "mfe_mae_measured": self.mfe_mae_measured,
        }


@dataclass
class EvaluationResult:
    status: str
    horizon: str
    fee_bps: float
    slippage_bps: float
    started_at: datetime
    finished_at: datetime | None = None
    coverage: Coverage = field(default_factory=Coverage)
    sample_size: int = 0
    positive_outcomes: int = 0
    negative_outcomes: int = 0
    zero_outcomes: int = 0
    win_rate: float | None = None
    gross: dict[str, float | None] = field(default_factory=dict)
    net: dict[str, float | None] = field(default_factory=dict)
    mfe: dict[str, float | None] = field(default_factory=dict)
    mae: dict[str, float | None] = field(default_factory=dict)
    by_category: dict[str, dict] = field(default_factory=dict)
    explanation: str = ""
    evaluation_id: int | None = None
    claim_ready: bool = False

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "horizon": self.horizon,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "round_trip_cost": round_trip_cost(self.fee_bps, self.slippage_bps),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sample_size": self.sample_size,
            "positive_outcomes": self.positive_outcomes,
            "negative_outcomes": self.negative_outcomes,
            "zero_outcomes": self.zero_outcomes,
            "win_rate": self.win_rate,
            "gross_return": self.gross,
            "net_return": self.net,
            "max_favorable_excursion": self.mfe,
            "max_adverse_excursion": self.mae,
            "by_category": self.by_category,
            "coverage": self.coverage.as_dict(),
            "claim_ready": self.claim_ready,
            "explanation": self.explanation,
            "evaluation_id": self.evaluation_id,
            "paper_trading_only": True,
        }


def _ordered(snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
    return sorted(
        [item for item in snapshots if _stamp(item.recorded_at) is not None],
        key=lambda item: (_stamp(item.recorded_at), item.id or 0),
    )


def _prior_category(
    snaps: list[MarketSnapshot],
    index: int,
    max_gap: timedelta,
) -> str:
    """Category uses only snapshots at or before the signal time."""

    if index <= 0:
        return NO_PRIOR
    current = snaps[index]
    previous = snaps[index - 1]
    start = _stamp(previous.recorded_at)
    end = _stamp(current.recorded_at)
    if start is None or end is None or end <= start:
        return NO_PRIOR
    if end - start > max_gap:
        return NO_PRIOR
    change = pct_change(valuation_usd(previous), valuation_usd(current))
    if change is None:
        return NO_PRIOR
    if change > 0:
        return PRIOR_UP
    if change < 0:
        return PRIOR_DOWN
    return PRIOR_UNCHANGED


def _path_extrema(path: list[MarketSnapshot], start_value: float) -> tuple[float | None, float | None]:
    moves: list[float] = []
    for item in path:
        value = valuation_usd(item)
        change = pct_change(start_value, value)
        if change is not None:
            moves.append(change)
    if len(moves) < 2:
        return None, None
    return max(moves), min(moves)


def _next_observation(
    snaps: list[MarketSnapshot],
    index: int,
    max_gap: timedelta,
) -> tuple[MarketSnapshot | None, list[MarketSnapshot], str | None]:
    if index + 1 >= len(snaps):
        return None, [snaps[index]], EXCLUDED_INSUFFICIENT
    current = snaps[index]
    nxt = snaps[index + 1]
    start = _stamp(current.recorded_at)
    end = _stamp(nxt.recorded_at)
    if start is None or end is None or end <= start:
        return None, [current], EXCLUDED_INSUFFICIENT
    if end - start > max_gap:
        return None, [current, nxt], EXCLUDED_GAP
    return nxt, [current, nxt], None


def _one_hour_outcome(
    snaps: list[MarketSnapshot],
    index: int,
    max_gap: timedelta,
    horizon: timedelta,
) -> tuple[MarketSnapshot | None, list[MarketSnapshot], str | None]:
    current = snaps[index]
    start = _stamp(current.recorded_at)
    if start is None:
        return None, [current], EXCLUDED_INSUFFICIENT
    path = [current]
    for j in range(index + 1, len(snaps)):
        prev = snaps[j - 1]
        nxt = snaps[j]
        prev_at = _stamp(prev.recorded_at)
        nxt_at = _stamp(nxt.recorded_at)
        if prev_at is None or nxt_at is None or nxt_at <= prev_at:
            return None, path, EXCLUDED_INSUFFICIENT
        if nxt_at - prev_at > max_gap:
            return None, path, EXCLUDED_GAP
        path.append(nxt)
        if nxt_at - start >= horizon:
            return nxt, path, None
    return None, path, EXCLUDED_INSUFFICIENT


def evaluate_token_snapshots(
    token_address: str,
    snapshots: list[MarketSnapshot],
    *,
    horizon: str,
    fee_bps: float,
    slippage_bps: float,
    max_gap_minutes: float,
) -> tuple[list[PathOutcome], Coverage]:
    coverage = Coverage(tokens_considered=1)
    ordered = _ordered(snapshots)
    if len(ordered) < 2:
        coverage.signals_considered = len(ordered)
        coverage.excluded_insufficient_history += len(ordered)
        return [], coverage

    max_gap = timedelta(minutes=max_gap_minutes)
    hour = timedelta(hours=1)
    cost = round_trip_cost(fee_bps, slippage_bps)
    outcomes: list[PathOutcome] = []

    for index, signal in enumerate(ordered):
        coverage.signals_considered += 1
        if signal.liquidity_usd is None:
            coverage.liquidity_missing_at_signal += 1
        start_value = valuation_usd(signal)
        if start_value is None:
            coverage.excluded_missing_valuation += 1
            continue

        category = _prior_category(ordered, index, max_gap)
        if horizon == HORIZON_NEXT:
            outcome_snap, path, reason = _next_observation(ordered, index, max_gap)
        elif horizon == HORIZON_HOUR:
            outcome_snap, path, reason = _one_hour_outcome(ordered, index, max_gap, hour)
        else:
            raise ValueError(f"Unsupported horizon: {horizon}")

        if reason == EXCLUDED_GAP:
            coverage.excluded_gap += 1
            continue
        if reason == EXCLUDED_INSUFFICIENT:
            coverage.excluded_insufficient_history += 1
            continue
        if outcome_snap is None:
            coverage.excluded_insufficient_history += 1
            continue

        end_value = valuation_usd(outcome_snap)
        if end_value is None:
            coverage.excluded_missing_outcome += 1
            continue

        gross = pct_change(start_value, end_value)
        if gross is None:
            coverage.excluded_missing_outcome += 1
            continue

        volume = pct_change(signal.volume_24h, outcome_snap.volume_24h)
        liquidity = pct_change(signal.liquidity_usd, outcome_snap.liquidity_usd)
        mfe, mae = _path_extrema(path, start_value)
        signal_at = _stamp(signal.recorded_at)
        outcome_at = _stamp(outcome_snap.recorded_at)
        assert signal_at is not None and outcome_at is not None
        elapsed = (outcome_at - signal_at).total_seconds() / 60.0
        net = net_return(gross, cost)
        positive: bool | None
        if gross > 0:
            positive = True
        elif gross < 0:
            positive = False
        else:
            positive = None

        coverage.outcomes_measured += 1
        coverage.valuation_changes_measured += 1
        if volume is not None:
            coverage.volume_changes_measured += 1
        if liquidity is not None:
            coverage.liquidity_changes_measured += 1
        if mfe is not None and mae is not None:
            coverage.mfe_mae_measured += 1

        outcomes.append(
            PathOutcome(
                token_address=token_address,
                signal_at=signal_at,
                outcome_at=outcome_at,
                category=category,
                elapsed_minutes=round(elapsed, 4),
                gross_return=round(gross, 6),
                net_return=round(net, 6) if net is not None else None,
                volume_change=round(volume, 6) if volume is not None else None,
                liquidity_change=round(liquidity, 6) if liquidity is not None else None,
                mfe=round(mfe, 6) if mfe is not None else None,
                mae=round(mae, 6) if mae is not None else None,
                positive=positive,
            )
        )

    return outcomes, coverage


def _category_block(rows: list[PathOutcome]) -> dict:
    positives = sum(1 for item in rows if item.positive is True)
    negatives = sum(1 for item in rows if item.positive is False)
    zeros = sum(1 for item in rows if item.positive is None)
    gross = [item.gross_return for item in rows if item.gross_return is not None]
    net = [item.net_return for item in rows if item.net_return is not None]
    return {
        "sample_size": len(rows),
        "positive_outcomes": positives,
        "negative_outcomes": negatives,
        "zero_outcomes": zeros,
        "win_rate": _win_rate(positives, negatives, zeros),
        "gross_return": _stats(gross),
        "net_return": _stats(net),
    }


def summarize_outcomes(
    outcomes: list[PathOutcome],
    coverage: Coverage,
    *,
    horizon: str,
    fee_bps: float,
    slippage_bps: float,
    min_sample_for_claims: int,
    started_at: datetime,
) -> EvaluationResult:
    positives = sum(1 for item in outcomes if item.positive is True)
    negatives = sum(1 for item in outcomes if item.positive is False)
    zeros = sum(1 for item in outcomes if item.positive is None)
    gross = [item.gross_return for item in outcomes if item.gross_return is not None]
    net = [item.net_return for item in outcomes if item.net_return is not None]
    mfe = [item.mfe for item in outcomes if item.mfe is not None]
    mae = [item.mae for item in outcomes if item.mae is not None]
    by_category: dict[str, dict] = {}
    for name in (PRIOR_UP, PRIOR_DOWN, PRIOR_UNCHANGED, NO_PRIOR):
        subset = [item for item in outcomes if item.category == name]
        if subset:
            by_category[name] = _category_block(subset)

    sample = len(outcomes)
    claim_ready = sample >= min_sample_for_claims
    cost = round_trip_cost(fee_bps, slippage_bps)
    explanation = (
        f"Market-only evaluation for horizon={horizon}. "
        f"Sample size={sample}. Win rate is the share of measured valuation returns that are positive. "
        f"Gross returns ignore costs. Estimated net returns subtract a hypothetical round-trip cost of "
        f"{cost:.4f} ({fee_bps} bps fee + {slippage_bps} bps slippage, each applied on entry and exit). "
        "These are not fills or execution prices. Gaps over the configured maximum are missing intervals, "
        "not 15-minute bars. Missing liquidity is omitted, never treated as zero. "
        "Results describe this stored sample only and do not guarantee future profitability."
    )
    if not claim_ready:
        explanation += (
            f" Sample size is below {min_sample_for_claims}, so this is descriptive only, not a predictive claim."
        )

    return EvaluationResult(
        status="completed",
        horizon=horizon,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        started_at=started_at,
        finished_at=utc_now(),
        coverage=coverage,
        sample_size=sample,
        positive_outcomes=positives,
        negative_outcomes=negatives,
        zero_outcomes=zeros,
        win_rate=_win_rate(positives, negatives, zeros),
        gross=_stats(gross),
        net=_stats(net),
        mfe=_stats(mfe),
        mae=_stats(mae),
        by_category=by_category,
        explanation=explanation,
        claim_ready=claim_ready,
    )


def _merge_coverage(total: Coverage, part: Coverage) -> None:
    total.tokens_considered += part.tokens_considered
    total.signals_considered += part.signals_considered
    total.outcomes_measured += part.outcomes_measured
    total.excluded_gap += part.excluded_gap
    total.excluded_missing_valuation += part.excluded_missing_valuation
    total.excluded_missing_outcome += part.excluded_missing_outcome
    total.excluded_insufficient_history += part.excluded_insufficient_history
    total.valuation_changes_measured += part.valuation_changes_measured
    total.volume_changes_measured += part.volume_changes_measured
    total.liquidity_changes_measured += part.liquidity_changes_measured
    total.liquidity_missing_at_signal += part.liquidity_missing_at_signal
    total.mfe_mae_measured += part.mfe_mae_measured


def run_historical_evaluation(
    session: Session,
    settings: Settings | None = None,
    *,
    horizon: str = HORIZON_NEXT,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    token_address: str | None = None,
    persist: bool = True,
) -> EvaluationResult:
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

    all_outcomes: list[PathOutcome] = []
    coverage = Coverage()
    for token in tokens:
        rows = by_token.get(token.id, [])
        token_outcomes, part = evaluate_token_snapshots(
            token.token_address,
            rows,
            horizon=horizon,
            fee_bps=fee,
            slippage_bps=slip,
            max_gap_minutes=settings.evaluation_max_gap_minutes,
        )
        _merge_coverage(coverage, part)
        all_outcomes.extend(token_outcomes)

    result = summarize_outcomes(
        all_outcomes,
        coverage,
        horizon=horizon,
        fee_bps=fee,
        slippage_bps=slip,
        min_sample_for_claims=settings.evaluation_min_sample_for_claims,
        started_at=started,
    )

    if persist:
        row = HistoricalEvaluation(
            started_at=result.started_at,
            finished_at=result.finished_at,
            status=result.status,
            horizon=horizon,
            fee_bps=fee,
            slippage_bps=slip,
            tokens_considered=coverage.tokens_considered,
            signals_considered=coverage.signals_considered,
            outcomes_measured=coverage.outcomes_measured,
            excluded_gap=coverage.excluded_gap,
            excluded_missing_valuation=coverage.excluded_missing_valuation,
            excluded_missing_outcome=coverage.excluded_missing_outcome,
            excluded_insufficient_history=coverage.excluded_insufficient_history,
            summary_json=json.dumps(result.as_dict()),
            explanation=result.explanation,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result.evaluation_id = row.id
        payload = result.as_dict()
        row.summary_json = json.dumps(payload)
        session.commit()
    return result


def latest_evaluation(session: Session) -> HistoricalEvaluation | None:
    return session.scalar(
        select(HistoricalEvaluation).order_by(HistoricalEvaluation.started_at.desc()).limit(1)
    )


def list_evaluations(session: Session, limit: int = 25, offset: int = 0) -> tuple[list[HistoricalEvaluation], int]:
    rows = list(
        session.scalars(
            select(HistoricalEvaluation).order_by(HistoricalEvaluation.started_at.desc())
        ).all()
    )
    return rows[offset : offset + limit], len(rows)


def get_evaluation(session: Session, evaluation_id: int) -> HistoricalEvaluation | None:
    return session.get(HistoricalEvaluation, evaluation_id)
