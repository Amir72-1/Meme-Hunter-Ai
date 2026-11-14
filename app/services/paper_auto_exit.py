"""Automatic PAPER_ONLY exits. Disabled by default. No DexScreener refresh or live sells."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    PaperAutoExitAttempt,
    PaperAutoExitCycle,
    PaperMarketQuote,
    PaperOrder,
    PaperPosition,
    PaperPositionObservation,
    PaperPortfolio,
    Token,
    TokenRiskAssessment,
)
from app.services.paper_book_lock import acquire_book_lock, bind_book_cycle, release_book_lock
from app.services.paper_money import ZERO, money, money_text, qty_text, quantity
from app.services.paper_quotes import QuoteRejected, load_executable_quote
from app.services.paper_trading import (
    _open_positions,
    _positions,
    ensure_default_portfolio,
    simulate_manual_order,
)
from app.services.risk_scoring import classify_measurements
from app.timeutil import as_utc, utc_now

TRIGGER_MANUAL = "MANUAL"
TRIGGER_SCHEDULED = "SCHEDULED"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_COMPLETED_WITH_REPORTING_ERROR = "completed_with_reporting_error"
EXIT_PENDING_DATA = "EXIT_PENDING_DATA"
HARD_QUOTE_BLOCKS = {
    "PAIR_MISMATCH",
    "PAIR_CHANGED",
    "SUSPICIOUS_DISCONTINUITY",
    "INVALID_PRICE",
    "INSUFFICIENT_LIQUIDITY",
}
PRECEDENCE = (
    "SECURITY_DETERIORATION",
    "STOP_LOSS",
    "TRAILING_STOP",
    "TAKE_PROFIT",
    "MAX_HOLDING",
)
NOTE = (
    "PAPER_ONLY simulated exit. Fills are hypothetical and are not market executions. "
    "Default stop/take-profit/trailing percentages are not claimed to be optimal or profitable. "
    "An unfilled stop is not successful risk protection."
)
HARD_RISK_BANDS = {"high", "critical"}


@dataclass
class ExitMonitor:
    position_id: int
    mint_address: str
    quantity: str
    average_cost_usd: str
    opened_at: datetime | None
    holding_hours: float | None
    observed_price_usd: str | None
    observed_return_pct: str | None
    high_water_usd: str | None
    stop_loss_usd: str | None
    take_profit_usd: str | None
    trailing_active: bool
    trailing_stop_usd: str | None
    quote_fresh: bool
    quote_reason: str | None
    quote_age_seconds: float | None
    freshness_basis: str | None
    pair_address: str | None
    security_status: str | None
    triggered: bool
    signal: str | None
    executable: bool
    pending_reason: str | None
    note: str = "Monitoring snapshot. Not a live order."

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.opened_at is not None:
            payload["opened_at"] = as_utc(self.opened_at).isoformat()
        return payload


@dataclass
class AutoExitSummary:
    cycle_id: int | None
    trigger: str
    status: str
    positions_evaluated: int = 0
    triggered: int = 0
    exited: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    orders_created: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str = NOTE


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        stamp = as_utc(value)
        return stamp.isoformat() if stamp else None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _dump_json(value: Any) -> str:
    return json.dumps(_json_safe(value), default=str)


def _has_durable_exits(cycle: PaperAutoExitCycle | None) -> bool:
    if cycle is None:
        return False
    return int(cycle.exited or 0) > 0 or int(cycle.orders_created or 0) > 0


def _finish_exit_cycle(
    session: Session,
    cycle: PaperAutoExitCycle,
    summary: AutoExitSummary,
    *,
    reporting_error: str | None = None,
) -> AutoExitSummary:
    cycle.finished_at = utc_now()
    errors = list(summary.errors)
    if reporting_error:
        errors.append({"detail": f"REPORTING_FAILURE: {reporting_error}"[:300]})
        cycle.status = (
            STATUS_COMPLETED_WITH_REPORTING_ERROR if _has_durable_exits(cycle) else STATUS_FAILED
        )
    else:
        cycle.status = STATUS_COMPLETED
    try:
        cycle.details_json = _dump_json({"attempts": summary.attempts})
        cycle.errors_json = _dump_json(errors)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        cycle = session.get(PaperAutoExitCycle, cycle.id) or cycle
        cycle.finished_at = utc_now()
        cycle.status = (
            STATUS_COMPLETED_WITH_REPORTING_ERROR if _has_durable_exits(cycle) else STATUS_FAILED
        )
        cycle.errors_json = json.dumps(
            [{"detail": f"REPORTING_FAILURE: {str(exc)[:220]}"}],
            default=str,
        )
        session.commit()
    summary.status = cycle.status
    summary.cycle_id = cycle.id
    summary.positions_evaluated = cycle.positions_evaluated
    summary.triggered = cycle.triggered
    summary.exited = cycle.exited
    summary.pending = cycle.pending
    summary.skipped = cycle.skipped
    summary.failed = cycle.failed
    summary.orders_created = cycle.orders_created
    summary.finished_at = cycle.finished_at
    summary.errors = json.loads(cycle.errors_json) if cycle.errors_json else errors
    return summary


def _config_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "paper_auto_exit_enabled": settings.paper_auto_exit_enabled,
        "paper_auto_exit_interval_seconds": settings.paper_auto_exit_interval_seconds,
        "paper_stop_loss_pct": settings.paper_stop_loss_pct,
        "paper_take_profit_pct": settings.paper_take_profit_pct,
        "paper_trailing_stop_pct": settings.paper_trailing_stop_pct,
        "paper_trailing_activate_pct": settings.paper_trailing_activate_pct,
        "paper_max_hold_hours": settings.paper_max_hold_hours,
        "paper_max_quote_age_seconds": settings.paper_max_quote_age_seconds,
        "paper_min_liquidity_usd": settings.paper_min_liquidity_usd,
        "paper_fee_bps": settings.paper_fee_bps,
        "paper_slippage_bps": settings.paper_slippage_bps,
        "optimized": False,
        "profitable": False,
        "wallet": False,
        "live_trading": False,
        "dexscreener_refresh": False,
    }


def _latest_quote_row(session: Session, token_id: int) -> PaperMarketQuote | None:
    return session.scalar(
        select(PaperMarketQuote)
        .where(PaperMarketQuote.token_id == token_id)
        .order_by(PaperMarketQuote.retrieved_at.desc(), PaperMarketQuote.id.desc())
        .limit(1)
    )


def _latest_security(session: Session, token_id: int) -> TokenRiskAssessment | None:
    return session.scalar(
        select(TokenRiskAssessment)
        .where(TokenRiskAssessment.token_id == token_id)
        .order_by(TokenRiskAssessment.scanned_at.desc(), TokenRiskAssessment.id.desc())
        .limit(1)
    )


def _accepted_high(
    session: Session,
    position_id: int,
    opened_at: datetime,
    now: datetime,
) -> Decimal | None:
    rows = list(
        session.scalars(
            select(PaperPositionObservation)
            .where(
                PaperPositionObservation.position_id == position_id,
                PaperPositionObservation.accepted == 1,
                PaperPositionObservation.observed_at >= opened_at,
                PaperPositionObservation.observed_at <= now,
            )
            .order_by(PaperPositionObservation.observed_at.asc(), PaperPositionObservation.id.asc())
        ).all()
    )
    high: Decimal | None = None
    for row in rows:
        if row.price_usd is None:
            continue
        price = money(row.price_usd)
        if high is None or price > high:
            high = price
    return high


def inspect_exit_quote(
    session: Session,
    token: Token,
    settings: Settings,
    now: datetime,
) -> tuple[Any | None, str | None]:
    stored = _latest_quote_row(session, token.id)
    if stored is not None and stored.reject_reason in HARD_QUOTE_BLOCKS:
        return None, stored.reject_reason
    try:
        quote = load_executable_quote(session, token, settings, now=now)
    except QuoteRejected as exc:
        return None, exc.reason
    if token.pair_address and quote.pair_address and token.pair_address != quote.pair_address:
        return None, "PAIR_MISMATCH"
    return quote, None


def _security_signal(
    session: Session,
    token: Token | None,
    settings: Settings,
    opened_at: datetime,
) -> tuple[str | None, str | None]:
    if token is None:
        return None, "MISSING_SECURITY"
    row = _latest_security(session, token.id)
    if row is None:
        return None, "MISSING_SECURITY"
    classified = classify_measurements(
        settings,
        completeness=row.completeness,
        confidence=row.confidence,
        observed_risk=row.observed_risk,
        observed_risk_band=row.observed_risk_band,
        stored_risk_score=row.risk_score,
        stored_risk_band=row.risk_band,
    )
    if not classified.classification_reliable:
        return None, classified.status
    band = (classified.risk_band or classified.observed_risk_band or "").lower()
    scanned = as_utc(row.scanned_at)
    if band in HARD_RISK_BANDS and scanned is not None and scanned >= opened_at:
        return "SECURITY_DETERIORATION", band
    return None, band or classified.status


def record_observation(
    session: Session,
    position: PaperPosition,
    token: Token | None,
    quote,
    reject_reason: str | None,
    high: Decimal | None,
    trailing_active: bool,
    now: datetime,
) -> PaperPositionObservation | None:
    observed_at = quote.observed_at if quote is not None else now
    existing = session.scalar(
        select(PaperPositionObservation)
        .where(
            PaperPositionObservation.position_id == position.id,
            PaperPositionObservation.observed_at == observed_at,
            PaperPositionObservation.paper_quote_id == (quote.quote_id if quote else None),
            PaperPositionObservation.snapshot_id == (quote.snapshot_id if quote else None),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    row = PaperPositionObservation(
        position_id=position.id,
        portfolio_id=position.portfolio_id,
        token_id=token.id if token is not None else position.token_id,
        mint_address=position.mint_address,
        observed_at=observed_at,
        retrieved_at=now,
        price_usd=float(quote.price_usd) if quote is not None else None,
        liquidity_usd=float(quote.liquidity_usd) if quote is not None and quote.liquidity_usd is not None else None,
        pair_address=quote.pair_address if quote is not None else None,
        source=quote.source if quote is not None else None,
        freshness_basis=quote.freshness_basis if quote is not None else None,
        paper_quote_id=quote.quote_id if quote is not None else None,
        snapshot_id=quote.snapshot_id if quote is not None else None,
        accepted=1 if quote is not None else 0,
        reject_reason=reject_reason,
        high_water_usd=float(high) if high is not None else None,
        trailing_active=1 if trailing_active else 0,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def evaluate_exit(
    session: Session,
    position: PaperPosition,
    settings: Settings,
    *,
    now: datetime | None = None,
    persist_mark: bool = True,
) -> ExitMonitor:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    opened = as_utc(position.opened_at) or now
    avg = money(position.average_cost_usd)
    qty = quantity(position.quantity)
    holding = max(0.0, (now - opened).total_seconds() / 3600.0)
    stop = money(avg * (Decimal("1") - money(settings.paper_stop_loss_pct)))
    take = money(avg * (Decimal("1") + money(settings.paper_take_profit_pct)))
    activate = money(avg * (Decimal("1") + money(settings.paper_trailing_activate_pct)))
    token = session.get(Token, position.token_id) if position.token_id else None
    if token is None:
        token = session.scalar(select(Token).where(Token.token_address == position.mint_address))
    quote, quote_reason = (None, "UNKNOWN_TOKEN")
    if token is not None:
        quote, quote_reason = inspect_exit_quote(session, token, settings, now)
    high = _accepted_high(session, position.id, opened, now)
    if quote is not None:
        observed = as_utc(quote.observed_at) or now
        if observed >= opened and (high is None or quote.price_usd > high):
            high = quote.price_usd
    trailing_active = high is not None and high >= activate
    trail_stop = (
        money(high * (Decimal("1") - money(settings.paper_trailing_stop_pct)))
        if trailing_active and high is not None
        else None
    )
    if persist_mark:
        record_observation(session, position, token, quote, quote_reason, high, trailing_active, now)

    signals: list[str] = []
    security_signal, security_status = _security_signal(session, token, settings, opened)
    if security_signal:
        signals.append(security_signal)
    if quote is not None:
        if quote.price_usd <= stop:
            signals.append("STOP_LOSS")
        if trail_stop is not None and quote.price_usd <= trail_stop:
            signals.append("TRAILING_STOP")
        if quote.price_usd >= take:
            signals.append("TAKE_PROFIT")
    if holding >= float(settings.paper_max_hold_hours):
        signals.append("MAX_HOLDING")
    signal = next((code for code in PRECEDENCE if code in signals), None)
    triggered = signal is not None
    executable = triggered and quote is not None
    pending = None
    if triggered and quote is None:
        pending = EXIT_PENDING_DATA
    ret = None
    if quote is not None and avg > ZERO:
        ret = money((quote.price_usd - avg) / avg)
    return ExitMonitor(
        position_id=position.id,
        mint_address=position.mint_address,
        quantity=qty_text(qty),
        average_cost_usd=money_text(avg),
        opened_at=opened,
        holding_hours=round(holding, 6),
        observed_price_usd=money_text(quote.price_usd) if quote is not None else None,
        observed_return_pct=format(ret, "f") if ret is not None else None,
        high_water_usd=money_text(high) if high is not None else None,
        stop_loss_usd=money_text(stop),
        take_profit_usd=money_text(take),
        trailing_active=trailing_active,
        trailing_stop_usd=money_text(trail_stop) if trail_stop is not None else None,
        quote_fresh=quote is not None,
        quote_reason=quote_reason,
        quote_age_seconds=quote.age_seconds if quote is not None else None,
        freshness_basis=quote.freshness_basis if quote is not None else None,
        pair_address=quote.pair_address if quote is not None else (token.pair_address if token else None),
        security_status=security_status,
        triggered=triggered,
        signal=signal,
        executable=executable,
        pending_reason=pending,
    )


def _pending_auto_sell(session: Session, portfolio_id: int, mint: str) -> PaperOrder | None:
    return session.scalar(
        select(PaperOrder).where(
            PaperOrder.portfolio_id == portfolio_id,
            PaperOrder.mint_address == mint,
            PaperOrder.side == "SELL",
            PaperOrder.status.in_(("PENDING", "NEW", "OPEN")),
            PaperOrder.client_order_id.like("auto-sell:%"),
        )
    )


def _sell_generation(session: Session, portfolio_id: int, mint: str) -> int:
    prior = list(
        session.scalars(
            select(PaperOrder).where(
                PaperOrder.portfolio_id == portfolio_id,
                PaperOrder.mint_address == mint,
                PaperOrder.client_order_id.like("auto-sell:%"),
                PaperOrder.status == "FILLED",
            )
        ).all()
    )
    return len(prior) + 1


def _record_attempt(
    session: Session,
    cycle: PaperAutoExitCycle,
    position: PaperPosition,
    *,
    outcome: str,
    reason: str,
    order_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> PaperAutoExitAttempt:
    row = PaperAutoExitAttempt(
        cycle_id=cycle.id,
        position_id=position.id,
        token_id=position.token_id,
        mint_address=position.mint_address,
        outcome=outcome,
        reason=reason[:64],
        order_id=order_id,
        quantity=position.quantity,
        details_json=json.dumps(_json_safe(details or {})),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _attempt_public(row: PaperAutoExitAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "position_id": row.position_id,
        "mint_address": row.mint_address,
        "outcome": row.outcome,
        "reason": row.reason,
        "order_id": row.order_id,
        "quantity": row.quantity,
        "details": json.loads(row.details_json) if row.details_json else None,
        "created_at": as_utc(row.created_at).isoformat() if as_utc(row.created_at) else None,
    }


def list_open_positions(session: Session, portfolio: PaperPortfolio) -> list[PaperPosition]:
    return [row for row in _open_positions(_positions(session, portfolio.id))]


def monitor_open_positions(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    persist_mark: bool = False,
) -> list[ExitMonitor]:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    portfolio = ensure_default_portfolio(session, settings)
    return [
        evaluate_exit(session, row, settings, now=now, persist_mark=persist_mark)
        for row in list_open_positions(session, portfolio)
    ]


def run_auto_exit_cycle(
    session: Session,
    settings: Settings | None = None,
    *,
    trigger: str = TRIGGER_MANUAL,
    now: datetime | None = None,
    holder: str | None = None,
) -> AutoExitSummary:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    holder = holder or f"exit:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    summary = AutoExitSummary(cycle_id=None, trigger=trigger, status=STATUS_SKIPPED, started_at=now)
    if not acquire_book_lock(session, settings, holder, now):
        summary.finished_at = utc_now()
        summary.errors.append({"detail": "Paper book lease is held. Fail closed; no exits submitted."})
        return summary

    cycle: PaperAutoExitCycle | None = None
    try:
        portfolio = ensure_default_portfolio(session, settings)
        cycle = PaperAutoExitCycle(
            trigger=trigger,
            status=STATUS_RUNNING,
            started_at=now,
            holder=holder,
            config_json=json.dumps(_config_snapshot(settings)),
            errors_json=json.dumps(summary.errors),
            note=NOTE,
        )
        session.add(cycle)
        session.commit()
        session.refresh(cycle)
        bind_book_cycle(session, cycle.id, "exit")
        summary.cycle_id = cycle.id
        summary.status = STATUS_RUNNING
        for position in list_open_positions(session, portfolio):
            cycle.positions_evaluated += 1
            session.commit()
            try:
                monitor = evaluate_exit(session, position, settings, now=now, persist_mark=True)
                details = monitor.as_dict()
                if not monitor.triggered:
                    attempt = _record_attempt(
                        session, cycle, position, outcome="WATCH", reason="NO_EXIT_SIGNAL", details=details
                    )
                    cycle.skipped += 1
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                cycle.triggered += 1
                if not monitor.executable:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        position,
                        outcome=EXIT_PENDING_DATA,
                        reason=monitor.quote_reason or EXIT_PENDING_DATA,
                        details=details,
                    )
                    cycle.pending += 1
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                if _pending_auto_sell(session, portfolio.id, position.mint_address) is not None:
                    attempt = _record_attempt(
                        session, cycle, position, outcome="SKIPPED", reason="PENDING_EXIT", details=details
                    )
                    cycle.skipped += 1
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                generation = _sell_generation(session, portfolio.id, position.mint_address)
                client_id = f"auto-sell:{portfolio.id}:{position.mint_address}:{position.id}:{monitor.signal}:{generation}"
                existing = session.scalar(
                    select(PaperOrder).where(
                        PaperOrder.portfolio_id == portfolio.id,
                        PaperOrder.client_order_id == client_id,
                    )
                )
                if existing is not None:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        position,
                        outcome="SKIPPED",
                        reason="DUPLICATE_CLIENT_ORDER",
                        order_id=existing.id,
                        details=details,
                    )
                    cycle.skipped += 1
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                qty = quantity(position.quantity)
                order = simulate_manual_order(
                    session,
                    settings,
                    mint_address=position.mint_address,
                    side="SELL",
                    quantity_value=format(qty, "f"),
                    client_order_id=client_id,
                    now=now,
                    extra_assumptions={
                        "auto_exit": True,
                        "cycle_id": cycle.id,
                        "signal": monitor.signal,
                        "execution": "hypothetical_paper_fill",
                        "market_execution": False,
                        "wallet": False,
                        "blockchain": False,
                        "unfilled_stop_is_not_protection": True,
                    },
                )
                if order.status != "FILLED":
                    attempt = _record_attempt(
                        session,
                        cycle,
                        position,
                        outcome="REJECT",
                        reason=order.reject_reason or "SIMULATE_REJECTED",
                        order_id=order.id,
                        details=details,
                    )
                    cycle.failed += 1
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                attempt = _record_attempt(
                    session,
                    cycle,
                    position,
                    outcome="EXITED",
                    reason=monitor.signal or "SIMULATED_SELL",
                    order_id=order.id,
                    details={
                        **details,
                        "order_status": order.status,
                        "observed_price_usd": order.observed_price_usd,
                        "simulated_price_usd": order.simulated_price_usd,
                        "market_execution": False,
                    },
                )
                cycle.exited += 1
                cycle.orders_created += 1
                summary.attempts.append(_attempt_public(attempt))
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                cycle = session.get(PaperAutoExitCycle, summary.cycle_id) or cycle
                attempt = _record_attempt(
                    session,
                    cycle,
                    position,
                    outcome="FAILED",
                    reason="EXIT_EXCEPTION",
                    details={"error": str(exc)[:300]},
                )
                cycle.failed += 1
                summary.errors.append({"mint_address": position.mint_address, "detail": str(exc)[:300]})
                summary.attempts.append(_attempt_public(attempt))
            session.commit()

        return _finish_exit_cycle(session, cycle, summary)
    except Exception as exc:
        if cycle is not None:
            session.rollback()
            cycle = session.get(PaperAutoExitCycle, cycle.id)
            if cycle is not None:
                return _finish_exit_cycle(session, cycle, summary, reporting_error=str(exc)[:220])
        summary.errors.append({"detail": str(exc)[:300]})
        summary.status = STATUS_FAILED
        summary.finished_at = summary.finished_at or utc_now()
        return summary
    finally:
        release_book_lock(session, holder)


def cycle_public(row: PaperAutoExitCycle) -> dict[str, Any]:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "positions_evaluated": row.positions_evaluated,
        "triggered": row.triggered,
        "exited": row.exited,
        "pending": row.pending,
        "skipped": row.skipped,
        "failed": row.failed,
        "orders_created": row.orders_created,
        "config": json.loads(row.config_json) if row.config_json else None,
        "errors": json.loads(row.errors_json) if row.errors_json else [],
        "note": row.note or NOTE,
        "market_execution": False,
        "wallet": False,
    }
