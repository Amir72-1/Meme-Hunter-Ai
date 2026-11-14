"""Controlled automatic paper BUY entries. PAPER_ONLY; no DexScreener refresh, wallets, or sells."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    PaperAutoEntryAttempt,
    PaperAutoEntryCycle,
    PaperOrder,
    PaperPortfolio,
    Token,
)
from app.services.paper_book_lock import (
    acquire_book_lock,
    bind_book_cycle,
    release_book_lock,
)
from app.services.paper_decision_engine import (
    BUY_CANDIDATE,
    REJECT,
    WAIT,
    evaluate_and_store,
    evaluate_token,
)
from app.services.paper_money import ZERO, money, money_text, quantity
from app.services.paper_quotes import load_executable_quote
from app.services.paper_simulator import plan_fill
from app.services.paper_trading import (
    _daily_realized_loss,
    _open_positions,
    _positions,
    build_portfolio_view,
    ensure_default_portfolio,
    simulate_manual_order,
)
from app.services.rpc_cooldown import get_cooldown_store
from app.timeutil import as_utc, utc_now

TRIGGER_MANUAL = "MANUAL"
TRIGGER_SCHEDULED = "SCHEDULED"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETED_WITH_REPORTING_ERROR = "completed_with_reporting_error"

NOTE = (
    "PAPER_ONLY simulated entry. Fills are hypothetical and are not market executions, "
    "wallet actions, or blockchain transactions. Automatic selling is not enabled."
)


@dataclass
class AutoEntrySummary:
    cycle_id: int | None
    trigger: str
    status: str
    tokens_evaluated: int = 0
    eligible: int = 0
    entered: int = 0
    rejected: int = 0
    waiting: int = 0
    failed: int = 0
    skipped: int = 0
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


def _has_durable_fills(cycle: PaperAutoEntryCycle | None) -> bool:
    if cycle is None:
        return False
    return int(cycle.entered or 0) > 0 or int(cycle.orders_created or 0) > 0


def _finish_entry_cycle(
    session: Session,
    cycle: PaperAutoEntryCycle,
    summary: AutoEntrySummary,
    *,
    reporting_error: str | None = None,
) -> AutoEntrySummary:
    cycle.finished_at = utc_now()
    errors = list(summary.errors)
    if reporting_error:
        errors.append({"detail": f"REPORTING_FAILURE: {reporting_error}"[:300]})
        cycle.status = (
            STATUS_COMPLETED_WITH_REPORTING_ERROR if _has_durable_fills(cycle) else STATUS_FAILED
        )
    else:
        cycle.status = STATUS_COMPLETED
    try:
        cycle.details_json = _dump_json({"attempts": summary.attempts})
        cycle.errors_json = _dump_json(errors)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        cycle = session.get(PaperAutoEntryCycle, cycle.id) or cycle
        cycle.finished_at = utc_now()
        cycle.status = (
            STATUS_COMPLETED_WITH_REPORTING_ERROR if _has_durable_fills(cycle) else STATUS_FAILED
        )
        cycle.errors_json = json.dumps(
            [{"detail": f"REPORTING_FAILURE: {str(exc)[:220]}"}],
            default=str,
        )
        session.commit()
    summary.status = cycle.status
    summary.cycle_id = cycle.id
    summary.tokens_evaluated = cycle.tokens_evaluated
    summary.eligible = cycle.eligible
    summary.entered = cycle.entered
    summary.rejected = cycle.rejected
    summary.waiting = cycle.waiting
    summary.failed = cycle.failed
    summary.skipped = cycle.skipped
    summary.orders_created = cycle.orders_created
    summary.finished_at = cycle.finished_at
    summary.errors = json.loads(cycle.errors_json) if cycle.errors_json else errors
    return summary


def reconcile_failed_cycle_with_fills(session: Session, cycle_id: int) -> dict[str, Any]:
    """Correct cycle status after a durable fill when only JSON reporting failed.

    Does not create orders, change quantities, or rewrite historical evaluations.
    """
    cycle = session.get(PaperAutoEntryCycle, cycle_id)
    if cycle is None:
        return {"repaired": False, "reason": "CYCLE_NOT_FOUND", "cycle_id": cycle_id}
    before = {
        "id": cycle.id,
        "status": cycle.status,
        "entered": cycle.entered,
        "orders_created": cycle.orders_created,
        "errors_json": cycle.errors_json,
    }
    if cycle.status != STATUS_FAILED or not _has_durable_fills(cycle):
        return {"repaired": False, "reason": "NOT_INCONSISTENT", "before": before}
    errors = json.loads(cycle.errors_json) if cycle.errors_json else []
    blob = json.dumps(errors)
    if "JSON serializable" not in blob and "REPORTING_FAILURE" not in blob:
        return {"repaired": False, "reason": "NOT_REPORTING_FAILURE", "before": before}
    cycle.status = STATUS_COMPLETED_WITH_REPORTING_ERROR
    errors.append(
        {
            "detail": (
                "RECONCILED_REPORTING_FAILURE: durable simulated fills were kept; "
                "cycle status no longer implies the book rolled back."
            )
        }
    )
    cycle.errors_json = _dump_json(errors)
    session.commit()
    return {
        "repaired": True,
        "reason": "REPORTING_FAILURE_AFTER_FILL",
        "before": before,
        "after_status": cycle.status,
        "fills_unchanged": True,
    }


def _config_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "paper_auto_entry_enabled": settings.paper_auto_entry_enabled,
        "paper_auto_entry_interval_minutes": settings.paper_auto_entry_interval_minutes,
        "paper_auto_entry_max_tokens": settings.paper_auto_entry_max_tokens,
        "paper_auto_entry_allocation_pct": settings.paper_auto_entry_allocation_pct,
        "paper_max_allocation_pct": settings.paper_max_allocation_pct,
        "paper_max_exposure_pct": settings.paper_max_exposure_pct,
        "paper_max_open_positions": settings.paper_max_open_positions,
        "paper_max_daily_realized_loss_usd": settings.paper_max_daily_realized_loss_usd,
        "paper_min_liquidity_usd": settings.paper_min_liquidity_usd,
        "paper_max_quote_age_seconds": settings.paper_max_quote_age_seconds,
        "paper_fee_bps": settings.paper_fee_bps,
        "paper_slippage_bps": settings.paper_slippage_bps,
        "opportunity_min_completeness": settings.opportunity_min_completeness,
        "opportunity_min_confidence": settings.opportunity_min_confidence,
        "opportunity_high_threshold": settings.opportunity_high_threshold,
        "security_min_completeness": settings.security_min_completeness,
        "security_min_confidence": settings.security_min_confidence,
        "automatic_selling": False,
        "dexscreener_refresh": False,
        "wallet": False,
        "live_trading": False,
    }


def acquire_entry_lock(session: Session, settings: Settings, holder: str, now: datetime) -> bool:
    return acquire_book_lock(session, settings, holder, now)


def bind_lock_cycle(session: Session, cycle_id: int) -> None:
    bind_book_cycle(session, cycle_id, "entry")


def release_entry_lock(session: Session, holder: str) -> None:
    release_book_lock(session, holder)


def _select_tokens(session: Session, settings: Settings, addresses: list[str] | None) -> list[Token]:
    cap = int(settings.paper_auto_entry_max_tokens)
    if addresses:
        tokens: list[Token] = []
        for address in addresses[:cap]:
            row = session.scalar(select(Token).where(Token.token_address == address.strip()))
            if row is not None and str(row.chain or "").lower() == "solana":
                tokens.append(row)
        return tokens
    return list(
        session.scalars(
            select(Token)
            .where(Token.chain == "solana")
            .order_by(Token.last_discovered_at.desc(), Token.id.desc())
            .limit(cap)
        ).all()
    )


def _open_position(session: Session, portfolio_id: int, mint: str):
    positions = _open_positions(_positions(session, portfolio_id))
    return next((row for row in positions if row.mint_address == mint), None)


def _pending_auto_order(session: Session, portfolio_id: int, mint: str) -> PaperOrder | None:
    return session.scalar(
        select(PaperOrder).where(
            PaperOrder.portfolio_id == portfolio_id,
            PaperOrder.mint_address == mint,
            PaperOrder.side == "BUY",
            PaperOrder.status.in_(("PENDING", "NEW", "OPEN")),
            PaperOrder.client_order_id.like("auto-buy:%"),
        )
    )


def _client_order_id(portfolio_id: int, mint: str, generation: int) -> str:
    return f"auto-buy:{portfolio_id}:{mint}:{generation}"


def _auto_fill_generation(session: Session, portfolio_id: int, mint: str) -> int:
    prior = list(
        session.scalars(
            select(PaperOrder).where(
                PaperOrder.portfolio_id == portfolio_id,
                PaperOrder.mint_address == mint,
                PaperOrder.client_order_id.like("auto-buy:%"),
                PaperOrder.status == "FILLED",
            )
        ).all()
    )
    return len(prior) + 1


def _gate_map(decision) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in json.loads(decision.gates_json)}


def revalidate_entry(
    session: Session,
    token: Token,
    settings: Settings,
    portfolio: PaperPortfolio,
    now: datetime,
) -> tuple[str, str, dict[str, Any]]:
    """Second-pass eligibility immediately before a simulated buy. No provider calls."""

    evaluation = evaluate_token(session, token, settings, now=now, portfolio=portfolio)
    details = {
        "decision_result": evaluation.result,
        "decision_reason": evaluation.reason,
        "gates": [gate.as_dict() for gate in evaluation.gates],
    }
    if evaluation.result == WAIT:
        return WAIT, evaluation.reason, details
    if evaluation.result != BUY_CANDIDATE:
        return REJECT, evaluation.reason, details

    view = build_portfolio_view(session, portfolio, settings, now=now)
    details["valuation_status"] = view.valuation_status
    details["equity_usd"] = money_text(view.equity) if view.equity is not None else None
    if view.valuation_status != "KNOWN" or view.equity is None:
        return REJECT, "UNKNOWN_EQUITY", details

    if _open_position(session, portfolio.id, token.token_address) is not None:
        return REJECT, "DUPLICATE_OPEN_POSITION", details
    if _pending_auto_order(session, portfolio.id, token.token_address) is not None:
        return REJECT, "PENDING_ENTRY", details

    quality = next((gate for gate in evaluation.gates if gate.name == "market_quality"), None)
    if quality is None or quality.code != "MARKET_QUALITY_CLEAN" or not quality.passed:
        return WAIT if quality and quality.verdict == WAIT else REJECT, (
            quality.code if quality else "MARKET_QUALITY_MISSING"
        ), details

    quote_gate = next((gate for gate in evaluation.gates if gate.name == "quote"), None)
    if quote_gate is None or not quote_gate.passed:
        verdict = quote_gate.verdict if quote_gate else WAIT
        return verdict, quote_gate.code if quote_gate else "MISSING_QUOTE", details

    quote = load_executable_quote(session, token, settings, now=now)
    if token.pair_address and quote.pair_address and token.pair_address != quote.pair_address:
        return REJECT, "PAIR_MISMATCH", details
    if evaluation.quote is not None and evaluation.quote.discontinuity_flag:
        return REJECT, "SUSPICIOUS_DISCONTINUITY", details
    if quote.liquidity_usd is None:
        return WAIT, "MISSING_LIQUIDITY", details
    if quote.liquidity_usd < money(settings.paper_min_liquidity_usd):
        return REJECT, "INSUFFICIENT_LIQUIDITY", details
    details["quote"] = {
        "source": quote.source,
        "age_seconds": quote.age_seconds,
        "freshness_basis": quote.freshness_basis,
        "pair_address": quote.pair_address,
        "paper_quote_id": quote.quote_id,
        "snapshot_id": quote.snapshot_id,
    }
    return BUY_CANDIDATE, "ELIGIBLE", details


def size_entry(
    session: Session,
    settings: Settings,
    portfolio: PaperPortfolio,
    token: Token,
    now: datetime,
) -> tuple[Decimal | None, str | None, dict[str, Any]]:
    view = build_portfolio_view(session, portfolio, settings, now=now)
    if view.valuation_status != "KNOWN" or view.equity is None:
        return None, "UNKNOWN_EQUITY", {"valuation_status": view.valuation_status}
    cash = view.cash_available
    equity = view.equity
    starting = money(portfolio.starting_cash_usd)
    positions = _positions(session, portfolio.id)
    open_rows = _open_positions(positions)
    existing = next((row for row in positions if row.mint_address == token.token_address), None)
    held = quantity(existing.quantity) if existing is not None else ZERO
    existing_cost = money(existing.cost_basis_usd) if existing is not None and held > ZERO else ZERO
    invested = sum((money(row.cost_basis_usd) for row in open_rows), ZERO)
    max_alloc = money(starting * money(settings.paper_max_allocation_pct))
    max_exposure = money(starting * money(settings.paper_max_exposure_pct))
    remaining_alloc = money(max_alloc - existing_cost)
    remaining_exposure = money(max_exposure - invested)
    target = money(equity * money(settings.paper_auto_entry_allocation_pct))
    notional = min(target, remaining_alloc, remaining_exposure, cash)
    meta = {
        "equity_usd": money_text(equity),
        "cash_available_usd": money_text(cash),
        "target_usd": money_text(target),
        "allocation_pct": settings.paper_auto_entry_allocation_pct,
        "max_allocation_usd": money_text(max_alloc),
        "max_exposure_usd": money_text(max_exposure),
        "sized_notional_usd": money_text(notional) if notional > ZERO else "0",
    }
    if notional <= ZERO:
        return None, "NONPOSITIVE_SIZE", meta
    quote = load_executable_quote(session, token, settings, now=now)
    plan = plan_fill(
        side="BUY",
        quote=quote,
        fee_bps=money(settings.paper_fee_bps),
        slippage_bps=money(settings.paper_slippage_bps),
        requested_notional_usd=notional,
    )
    cost = money(-plan.cash_delta_usd)
    if cost > cash:
        scaled = money(notional * cash / cost) if cost > ZERO else ZERO
        if scaled <= ZERO or scaled >= notional:
            meta["required_cash_usd"] = money_text(cost)
            return None, "INSUFFICIENT_CASH", meta
        plan = plan_fill(
            side="BUY",
            quote=quote,
            fee_bps=money(settings.paper_fee_bps),
            slippage_bps=money(settings.paper_slippage_bps),
            requested_notional_usd=scaled,
        )
        cost = money(-plan.cash_delta_usd)
        notional = scaled
        if cost > cash:
            meta["required_cash_usd"] = money_text(cost)
            return None, "INSUFFICIENT_CASH", meta
    meta["sized_notional_usd"] = money_text(notional)
    meta["estimated_cash_out_usd"] = money_text(cost)
    meta["fee_usd"] = money_text(plan.fee_usd)
    meta["slippage_usd"] = money_text(plan.slippage_usd)
    meta["simulated_price_usd"] = money_text(plan.simulated_price_usd)
    loss = _daily_realized_loss(session, portfolio.id, now)
    if loss <= -money(settings.paper_max_daily_realized_loss_usd) and money(
        settings.paper_max_daily_realized_loss_usd
    ) > ZERO:
        return None, "MAX_DAILY_LOSS", meta
    return notional, None, meta


def _record_attempt(
    session: Session,
    cycle: PaperAutoEntryCycle,
    *,
    token: Token,
    outcome: str,
    reason: str,
    decision_id: int | None = None,
    paper_decision_id: int | None = None,
    order_id: int | None = None,
    notional: Decimal | None = None,
    details: dict[str, Any] | None = None,
) -> PaperAutoEntryAttempt:
    row = PaperAutoEntryAttempt(
        cycle_id=cycle.id,
        token_id=token.id,
        mint_address=token.token_address,
        decision_id=decision_id,
        paper_decision_id=paper_decision_id,
        order_id=order_id,
        outcome=outcome,
        reason=reason[:64],
        notional_usd=money_text(notional) if notional is not None else None,
        details_json=json.dumps(_json_safe(details or {})),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _count(cycle: PaperAutoEntryCycle, outcome: str) -> None:
    if outcome == "ENTERED":
        cycle.entered += 1
        cycle.orders_created += 1
        cycle.eligible += 1
    elif outcome == "WAIT":
        cycle.waiting += 1
    elif outcome == "REJECT":
        cycle.rejected += 1
    elif outcome == "SKIPPED":
        cycle.skipped += 1
    elif outcome == "FAILED":
        cycle.failed += 1
    elif outcome == "ELIGIBLE_FAIL":
        cycle.eligible += 1
        cycle.failed += 1


def run_auto_entry_cycle(
    session: Session,
    settings: Settings | None = None,
    *,
    trigger: str = TRIGGER_MANUAL,
    token_addresses: list[str] | None = None,
    now: datetime | None = None,
    holder: str | None = None,
) -> AutoEntrySummary:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    holder = holder or f"pid:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    summary = AutoEntrySummary(cycle_id=None, trigger=trigger, status=STATUS_SKIPPED, started_at=now)
    if get_cooldown_store().snapshot(now=now).active:
        summary.errors.append(
            {
                "detail": "Solana RPC cooldown is active. Auto-entry does not call RPC or DexScreener; "
                "the cycle continues on stored evidence only."
            }
        )
    if not acquire_entry_lock(session, settings, holder, now):
        summary.status = STATUS_SKIPPED
        summary.finished_at = utc_now()
        summary.errors.append({"detail": "Another auto-entry cycle holds the lease. Fail closed."})
        return summary

    cycle: PaperAutoEntryCycle | None = None
    try:
        portfolio = ensure_default_portfolio(session, settings)
        cycle = PaperAutoEntryCycle(
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
        bind_lock_cycle(session, cycle.id)
        summary.cycle_id = cycle.id
        summary.status = STATUS_RUNNING
        tokens = _select_tokens(session, settings, token_addresses)
        for token in tokens:
            cycle.tokens_evaluated += 1
            session.commit()
            try:
                decision, _ = evaluate_and_store(
                    session,
                    token.token_address,
                    settings,
                    force=True,
                    now=now,
                )
                if decision.result == WAIT:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome="WAIT",
                        reason=decision.reason,
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        details={"gates": _gate_map(decision)},
                    )
                    _count(cycle, "WAIT")
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                if decision.result != BUY_CANDIDATE:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome="REJECT",
                        reason=decision.reason,
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        details={"gates": _gate_map(decision)},
                    )
                    _count(cycle, "REJECT")
                    summary.attempts.append(_attempt_public(attempt))
                    continue

                verdict, reason, details = revalidate_entry(session, token, settings, portfolio, now)
                details["decision_id"] = decision.id
                if verdict == WAIT:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome="WAIT",
                        reason=reason,
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        details=details,
                    )
                    _count(cycle, "WAIT")
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                if verdict != BUY_CANDIDATE:
                    outcome = "SKIPPED" if reason in {"DUPLICATE_OPEN_POSITION", "PENDING_ENTRY"} else "REJECT"
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome=outcome,
                        reason=reason,
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        details=details,
                    )
                    _count(cycle, outcome)
                    summary.attempts.append(_attempt_public(attempt))
                    continue

                notional, size_reason, size_meta = size_entry(session, settings, portfolio, token, now)
                details["sizing"] = size_meta
                if notional is None:
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome="REJECT",
                        reason=size_reason or "SIZE_FAILED",
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        details=details,
                    )
                    _count(cycle, "REJECT")
                    summary.attempts.append(_attempt_public(attempt))
                    continue

                generation = _auto_fill_generation(session, portfolio.id, token.token_address)
                client_id = _client_order_id(portfolio.id, token.token_address, generation)
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
                        token=token,
                        outcome="SKIPPED",
                        reason="DUPLICATE_CLIENT_ORDER",
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        order_id=existing.id,
                        notional=notional,
                        details=details,
                    )
                    _count(cycle, "SKIPPED")
                    summary.attempts.append(_attempt_public(attempt))
                    continue

                order = simulate_manual_order(
                    session,
                    settings,
                    mint_address=token.token_address,
                    side="BUY",
                    notional_usd=money_text(notional),
                    client_order_id=client_id,
                    now=now,
                    extra_assumptions={
                        "auto_entry": True,
                        "cycle_id": cycle.id,
                        "decision_id": decision.id,
                        "trigger": trigger,
                        "execution": "hypothetical_paper_fill",
                        "market_execution": False,
                        "wallet": False,
                        "blockchain": False,
                    },
                )
                if order.status != "FILLED":
                    attempt = _record_attempt(
                        session,
                        cycle,
                        token=token,
                        outcome="REJECT",
                        reason=order.reject_reason or "SIMULATE_REJECTED",
                        decision_id=decision.id,
                        paper_decision_id=decision.paper_decision_id,
                        order_id=order.id,
                        notional=notional,
                        details=details,
                    )
                    _count(cycle, "REJECT")
                    summary.attempts.append(_attempt_public(attempt))
                    continue
                attempt = _record_attempt(
                    session,
                    cycle,
                    token=token,
                    outcome="ENTERED",
                    reason="SIMULATED_FILL",
                    decision_id=decision.id,
                    paper_decision_id=decision.paper_decision_id,
                    order_id=order.id,
                    notional=notional,
                    details={
                        **details,
                        "order_status": order.status,
                        "observed_price_usd": order.observed_price_usd,
                        "simulated_price_usd": order.simulated_price_usd,
                        "fill_quantity": order.fill_quantity,
                        "fee_usd": order.fee_usd,
                        "market_execution": False,
                    },
                )
                _count(cycle, "ENTERED")
                summary.attempts.append(_attempt_public(attempt))
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                cycle = session.get(PaperAutoEntryCycle, summary.cycle_id) or cycle
                attempt = _record_attempt(
                    session,
                    cycle,
                    token=token,
                    outcome="FAILED",
                    reason="ENTRY_EXCEPTION",
                    details={"error": str(exc)[:300]},
                )
                _count(cycle, "FAILED")
                summary.errors.append({"token_address": token.token_address, "detail": str(exc)[:300]})
                summary.attempts.append(_attempt_public(attempt))
            session.commit()

        return _finish_entry_cycle(session, cycle, summary)
    except Exception as exc:
        if cycle is not None:
            session.rollback()
            cycle = session.get(PaperAutoEntryCycle, cycle.id)
            if cycle is not None:
                return _finish_entry_cycle(session, cycle, summary, reporting_error=str(exc)[:220])
        summary.errors.append({"detail": str(exc)[:300]})
        summary.status = STATUS_FAILED
        summary.finished_at = summary.finished_at or utc_now()
        return summary
    finally:
        release_entry_lock(session, holder)


def _attempt_public(row: PaperAutoEntryAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "mint_address": row.mint_address,
        "decision_id": row.decision_id,
        "paper_decision_id": row.paper_decision_id,
        "order_id": row.order_id,
        "outcome": row.outcome,
        "reason": row.reason,
        "notional_usd": row.notional_usd,
        "details": json.loads(row.details_json) if row.details_json else None,
        "created_at": as_utc(row.created_at).isoformat() if as_utc(row.created_at) else None,
    }


def cycle_public(row: PaperAutoEntryCycle) -> dict[str, Any]:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "tokens_evaluated": row.tokens_evaluated,
        "eligible": row.eligible,
        "entered": row.entered,
        "rejected": row.rejected,
        "waiting": row.waiting,
        "failed": row.failed,
        "skipped": row.skipped,
        "orders_created": row.orders_created,
        "config": json.loads(row.config_json) if row.config_json else None,
        "errors": json.loads(row.errors_json) if row.errors_json else [],
        "note": row.note or NOTE,
        "automatic_selling": False,
        "market_execution": False,
    }


def summary_from_cycle(row: PaperAutoEntryCycle, attempts: list[PaperAutoEntryAttempt] | None = None) -> AutoEntrySummary:
    items = attempts or []
    return AutoEntrySummary(
        cycle_id=row.id,
        trigger=row.trigger,
        status=row.status,
        tokens_evaluated=row.tokens_evaluated,
        eligible=row.eligible,
        entered=row.entered,
        rejected=row.rejected,
        waiting=row.waiting,
        failed=row.failed,
        skipped=row.skipped,
        orders_created=row.orders_created,
        attempts=[_attempt_public(item) for item in items],
        errors=json.loads(row.errors_json) if row.errors_json else [],
        started_at=row.started_at,
        finished_at=row.finished_at,
        note=row.note or NOTE,
    )
