"""Investigate stored DexScreener snapshots. Flags are not proof of fraud or safety."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DataQualityFinding, DataQualityScan, MarketSnapshot, Token
from app.services.historical_evaluation import valuation_usd
from app.timeutil import as_utc, utc_now

PAIR_CHANGED = "PAIR_CHANGED"
MISSING_LIQUIDITY = "MISSING_LIQUIDITY"
MISSING_VALUATION = "MISSING_VALUATION"
STALE_SNAPSHOT = "STALE_SNAPSHOT"
DUPLICATE_VALUATION = "DUPLICATE_VALUATION"
EXTREME_PRICE_MOVEMENT = "EXTREME_PRICE_MOVEMENT"
EXTREME_REVERSAL = "EXTREME_REVERSAL"
LIQUIDITY_DISLOCATION = "LIQUIDITY_DISLOCATION"
POSSIBLE_QUOTE_REGIME_CHANGE = "POSSIBLE_QUOTE_REGIME_CHANGE"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
VALUATION_SOURCE_CHANGED = "VALUATION_SOURCE_CHANGED"

CLEAN = "CLEAN"
WARNING = "WARNING"
SUSPICIOUS = "SUSPICIOUS"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

SUSPICIOUS_FLAGS = {
    EXTREME_REVERSAL,
    POSSIBLE_QUOTE_REGIME_CHANGE,
    LIQUIDITY_DISLOCATION,
}
WARNING_FLAGS = {
    PAIR_CHANGED,
    MISSING_LIQUIDITY,
    MISSING_VALUATION,
    STALE_SNAPSHOT,
    DUPLICATE_VALUATION,
    EXTREME_PRICE_MOVEMENT,
    VALUATION_SOURCE_CHANGED,
}


def valuation_basis(snapshot: MarketSnapshot) -> str | None:
    if snapshot.market_cap is not None and snapshot.market_cap > 0:
        return "market_cap"
    if snapshot.fdv is not None and snapshot.fdv > 0:
        return "fdv"
    return None


def _ratio(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0 or end < 0:
        return None
    return end / start


def _ordered(snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
    return sorted(
        [item for item in snapshots if as_utc(item.recorded_at) is not None],
        key=lambda item: (as_utc(item.recorded_at), item.id or 0),
    )


@dataclass
class QualityFlag:
    code: str
    detail: str
    from_at: datetime | None = None
    to_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "detail": self.detail,
            "from_at": self.from_at.isoformat() if self.from_at else None,
            "to_at": self.to_at.isoformat() if self.to_at else None,
        }


@dataclass
class LiquidityCoverage:
    total_observations: int = 0
    non_null_liquidity: int = 0
    missing_liquidity: int = 0

    @property
    def missing_pct(self) -> float | None:
        if self.total_observations <= 0:
            return None
        return round(self.missing_liquidity / self.total_observations, 6)

    def as_dict(self) -> dict:
        return {
            "total_observations": self.total_observations,
            "non_null_liquidity": self.non_null_liquidity,
            "missing_liquidity": self.missing_liquidity,
            "missing_pct": self.missing_pct,
            "note": "Missing liquidity is not treated as zero.",
        }


@dataclass
class TokenQualityResult:
    token_address: str
    symbol: str | None
    classification: str
    flags: list[QualityFlag] = field(default_factory=list)
    snapshot_count: int = 0
    liquidity: LiquidityCoverage = field(default_factory=LiquidityCoverage)
    pair_changes: int = 0
    valuation_source_changes: int = 0
    extreme_moves: int = 0
    extreme_reversals: int = 0
    explanation: str = ""

    def flag_codes(self) -> list[str]:
        return [item.code for item in self.flags]


def classify_from_flags(flags: list[QualityFlag]) -> str:
    codes = {item.code for item in flags}
    if INSUFFICIENT_HISTORY in codes:
        return INSUFFICIENT_DATA
    if codes & SUSPICIOUS_FLAGS:
        return SUSPICIOUS
    if codes & WARNING_FLAGS:
        return WARNING
    return CLEAN


def _explain(classification: str, flags: list[QualityFlag]) -> str:
    if classification == CLEAN:
        return (
            "CLEAN means stored snapshots passed the configured quality checks. "
            "It is not a safety rating and not a profitability forecast."
        )
    if classification == INSUFFICIENT_DATA:
        return "INSUFFICIENT_DATA: fewer than two timestamped snapshots, so interval checks cannot run."
    names = ", ".join(sorted({item.code for item in flags}))
    if classification == SUSPICIOUS:
        return (
            f"SUSPICIOUS investigation flags: {names}. "
            "These are not proof of manipulation. Extreme DexScreener prints may be quote-regime changes, "
            "not executable trades."
        )
    return f"WARNING investigation flags: {names}. Missing or inconsistent fields reduce tradeability confidence."


def analyze_token_snapshots(
    token_address: str,
    snapshots: list[MarketSnapshot],
    settings: Settings,
    *,
    symbol: str | None = None,
    now: datetime | None = None,
) -> TokenQualityResult:
    now = now or utc_now()
    ordered = _ordered(snapshots)
    liquidity = LiquidityCoverage(total_observations=len(ordered))
    for snap in ordered:
        if snap.liquidity_usd is None:
            liquidity.missing_liquidity += 1
        else:
            liquidity.non_null_liquidity += 1

    flags: list[QualityFlag] = []
    if len(ordered) < 2:
        flags.append(
            QualityFlag(
                INSUFFICIENT_HISTORY,
                f"Only {len(ordered)} timestamped snapshot(s); consecutive checks need at least two.",
            )
        )
        result = TokenQualityResult(
            token_address=token_address,
            symbol=symbol,
            classification=INSUFFICIENT_DATA,
            flags=flags,
            snapshot_count=len(ordered),
            liquidity=liquidity,
            explanation=_explain(INSUFFICIENT_DATA, flags),
        )
        return result

    stale_after = timedelta(hours=settings.quality_stale_hours)
    last_at = as_utc(ordered[-1].recorded_at)
    if last_at is not None and now - last_at > stale_after:
        flags.append(
            QualityFlag(
                STALE_SNAPSHOT,
                f"Latest snapshot is older than {settings.quality_stale_hours} hours.",
                from_at=last_at,
            )
        )

    if liquidity.missing_liquidity:
        flags.append(
            QualityFlag(
                MISSING_LIQUIDITY,
                (
                    f"{liquidity.missing_liquidity}/{liquidity.total_observations} snapshots "
                    f"have null liquidity ({liquidity.missing_pct:.2%} missing). Not treated as zero."
                ),
            )
        )

    extreme_highs: list[tuple[datetime, float]] = []
    pair_changes = 0
    source_changes = 0
    extreme_moves = 0

    for prev, nxt in zip(ordered, ordered[1:]):
        prev_at = as_utc(prev.recorded_at)
        nxt_at = as_utc(nxt.recorded_at)
        if prev_at and nxt_at and nxt_at - prev_at > stale_after:
            flags.append(
                QualityFlag(
                    STALE_SNAPSHOT,
                    f"Gap of {(nxt_at - prev_at).total_seconds() / 3600:.2f} hours exceeds stale threshold.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        if (prev.pair_address or "") != (nxt.pair_address or ""):
            pair_changes += 1
            flags.append(
                QualityFlag(
                    PAIR_CHANGED,
                    f"pair_address changed from {prev.pair_address!r} to {nxt.pair_address!r}.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        start_basis = valuation_basis(prev)
        end_basis = valuation_basis(nxt)
        if start_basis is None:
            flags.append(
                QualityFlag(
                    MISSING_VALUATION,
                    "Starting snapshot has no usable market cap or FDV.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        if end_basis is None:
            flags.append(
                QualityFlag(
                    MISSING_VALUATION,
                    "Ending snapshot has no usable market cap or FDV.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        if start_basis and end_basis and start_basis != end_basis:
            source_changes += 1
            flags.append(
                QualityFlag(
                    VALUATION_SOURCE_CHANGED,
                    f"Valuation source changed from {start_basis} to {end_basis}.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )

        start_val = valuation_usd(prev)
        end_val = valuation_usd(nxt)
        val_ratio = _ratio(start_val, end_val)
        px_ratio = _ratio(prev.price_usd, nxt.price_usd)
        if start_val is not None and end_val is not None and start_val == end_val:
            same_price = prev.price_usd is None or nxt.price_usd is None or prev.price_usd == nxt.price_usd
            if same_price:
                flags.append(
                    QualityFlag(
                        DUPLICATE_VALUATION,
                        f"Repeated valuation {start_val} across consecutive snapshots.",
                        from_at=prev_at,
                        to_at=nxt_at,
                    )
                )

        extreme = False
        multiple = settings.quality_extreme_move_multiple
        if val_ratio is not None and val_ratio > multiple:
            extreme = True
            extreme_moves += 1
            flags.append(
                QualityFlag(
                    EXTREME_PRICE_MOVEMENT,
                    f"Valuation ratio {val_ratio:.4f} exceeds {multiple}x in one interval.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        if px_ratio is not None and px_ratio > multiple:
            extreme = True
            extreme_moves += 1
            flags.append(
                QualityFlag(
                    EXTREME_PRICE_MOVEMENT,
                    f"price_usd ratio {px_ratio:.4f} exceeds {multiple}x in one interval.",
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        if extreme and end_val is not None and nxt_at is not None:
            extreme_highs.append((nxt_at, end_val))

        liq_ratio = _ratio(prev.liquidity_usd, nxt.liquidity_usd)
        if liq_ratio is not None and (
            liq_ratio > settings.quality_liquidity_dislocation_ratio
            or (liq_ratio > 0 and 1 / liq_ratio > settings.quality_liquidity_dislocation_ratio)
        ):
            flags.append(
                QualityFlag(
                    LIQUIDITY_DISLOCATION,
                    (
                        f"Liquidity ratio {liq_ratio:.4f} exceeds "
                        f"{settings.quality_liquidity_dislocation_ratio}x. "
                        "Both endpoints had non-null liquidity; missing values were not used."
                    ),
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )
        pair_same = (prev.pair_address or "") == (nxt.pair_address or "")
        if extreme and pair_same and liq_ratio is not None and liq_ratio > settings.quality_liquidity_dislocation_ratio:
            flags.append(
                QualityFlag(
                    POSSIBLE_QUOTE_REGIME_CHANGE,
                    (
                        "Extreme valuation move with unchanged pair_address and a large liquidity jump. "
                        "Unchanged pair is not proof the quote is executable."
                    ),
                    from_at=prev_at,
                    to_at=nxt_at,
                )
            )

    reversal_floor = 1.0 - settings.quality_extreme_reversal
    extreme_reversals = 0
    for high_at, high_val in extreme_highs:
        for snap in ordered:
            stamp = as_utc(snap.recorded_at)
            if stamp is None or stamp <= high_at:
                continue
            later = valuation_usd(snap)
            if later is None or high_val <= 0:
                continue
            if later / high_val <= reversal_floor:
                extreme_reversals += 1
                flags.append(
                    QualityFlag(
                        EXTREME_REVERSAL,
                        (
                            f"After a >{settings.quality_extreme_move_multiple}x increase, valuation "
                            f"fell {1 - later / high_val:.2%} from {high_val} to {later}."
                        ),
                        from_at=high_at,
                        to_at=stamp,
                    )
                )
                flags.append(
                    QualityFlag(
                        POSSIBLE_QUOTE_REGIME_CHANGE,
                        "Extreme increase followed by a >=90% reversal. May be a quote-regime change.",
                        from_at=high_at,
                        to_at=stamp,
                    )
                )
                break

    # Deduplicate identical flag codes with same timestamps while keeping details.
    unique: list[QualityFlag] = []
    seen: set[tuple] = set()
    for item in flags:
        key = (item.code, item.from_at, item.to_at, item.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    classification = classify_from_flags(unique)
    return TokenQualityResult(
        token_address=token_address,
        symbol=symbol,
        classification=classification,
        flags=unique,
        snapshot_count=len(ordered),
        liquidity=liquidity,
        pair_changes=pair_changes,
        valuation_source_changes=source_changes,
        extreme_moves=extreme_moves,
        extreme_reversals=extreme_reversals,
        explanation=_explain(classification, unique),
    )


def run_quality_scan(
    session: Session,
    settings: Settings | None = None,
    *,
    token_address: str | None = None,
    persist: bool = True,
    now: datetime | None = None,
) -> dict:
    settings = settings or get_settings()
    now = now or utc_now()
    started = utc_now()
    query = select(Token)
    if token_address:
        query = query.where(Token.token_address == token_address)
    tokens = list(session.scalars(query).all())
    snaps = list(session.scalars(select(MarketSnapshot)).all())
    by_token: dict[int, list[MarketSnapshot]] = {}
    for snap in snaps:
        by_token.setdefault(snap.token_id, []).append(snap)

    results: list[TokenQualityResult] = []
    liquidity = LiquidityCoverage()
    counts = {CLEAN: 0, WARNING: 0, SUSPICIOUS: 0, INSUFFICIENT_DATA: 0}
    flag_counts: dict[str, int] = {}
    for token in tokens:
        rows = by_token.get(token.id, [])
        result = analyze_token_snapshots(
            token.token_address,
            rows,
            settings,
            symbol=token.symbol,
            now=now,
        )
        results.append(result)
        liquidity.total_observations += result.liquidity.total_observations
        liquidity.non_null_liquidity += result.liquidity.non_null_liquidity
        liquidity.missing_liquidity += result.liquidity.missing_liquidity
        counts[result.classification] = counts.get(result.classification, 0) + 1
        for code in set(result.flag_codes()):
            flag_counts[code] = flag_counts.get(code, 0) + 1

    finished = utc_now()
    payload = {
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "tokens_scanned": len(results),
        "snapshots_scanned": liquidity.total_observations,
        "classifications": counts,
        "flag_token_counts": flag_counts,
        "liquidity": liquidity.as_dict(),
        "thresholds": {
            "extreme_move_multiple": settings.quality_extreme_move_multiple,
            "extreme_reversal": settings.quality_extreme_reversal,
            "stale_hours": settings.quality_stale_hours,
            "liquidity_dislocation_ratio": settings.quality_liquidity_dislocation_ratio,
        },
        "explanation": (
            "Quality scan of stored DexScreener snapshots only. No live APIs. "
            "CLEAN is not a buy signal. SUSPICIOUS flags are investigations, not fraud findings. "
            "Missing liquidity is never treated as zero."
        ),
        "paper_trading_only": True,
    }
    scan_id = None
    if persist:
        scan = DataQualityScan(
            started_at=started,
            finished_at=finished,
            status="completed",
            tokens_scanned=len(results),
            snapshots_scanned=liquidity.total_observations,
            clean_count=counts[CLEAN],
            warning_count=counts[WARNING],
            suspicious_count=counts[SUSPICIOUS],
            insufficient_data_count=counts[INSUFFICIENT_DATA],
            liquidity_non_null=liquidity.non_null_liquidity,
            liquidity_missing=liquidity.missing_liquidity,
            summary_json=json.dumps(payload),
            explanation=payload["explanation"],
        )
        session.add(scan)
        session.flush()
        token_by_address = {token.token_address: token for token in tokens}
        for result in results:
            token = token_by_address[result.token_address]
            session.add(
                DataQualityFinding(
                    scan_id=scan.id,
                    token_id=token.id,
                    classification=result.classification,
                    flags_json=json.dumps([item.as_dict() for item in result.flags]),
                    snapshot_count=result.snapshot_count,
                    liquidity_non_null=result.liquidity.non_null_liquidity,
                    liquidity_missing=result.liquidity.missing_liquidity,
                    explanation=result.explanation,
                    assessed_at=finished,
                )
            )
        session.commit()
        scan_id = scan.id
        payload["scan_id"] = scan_id
        scan.summary_json = json.dumps(payload)
        session.commit()
    payload["scan_id"] = scan_id
    payload["items"] = [
        {
            "token_address": item.token_address,
            "symbol": item.symbol,
            "classification": item.classification,
            "flags": [flag.as_dict() for flag in item.flags],
            "snapshot_count": item.snapshot_count,
            "liquidity": item.liquidity.as_dict(),
            "explanation": item.explanation,
        }
        for item in results
    ]
    return payload


def latest_quality_scan(session: Session) -> DataQualityScan | None:
    return session.scalar(select(DataQualityScan).order_by(DataQualityScan.started_at.desc()).limit(1))


def latest_finding_for_token(session: Session, token_id: int) -> DataQualityFinding | None:
    return session.scalar(
        select(DataQualityFinding)
        .where(DataQualityFinding.token_id == token_id)
        .order_by(DataQualityFinding.assessed_at.desc(), DataQualityFinding.id.desc())
        .limit(1)
    )
