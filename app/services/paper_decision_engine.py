"""Deterministic BUY_CANDIDATE / WAIT / REJECT from stored evidence. No orders or provider calls."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    IntelligenceScan,
    MarketSnapshot,
    OpportunityAssessment,
    PaperDecision,
    PaperOpportunityDecision,
    PaperMarketQuote,
    PaperPortfolio,
    PaperPosition,
    Token,
    TokenRiskAssessment,
)
from app.services.market_data_quality import (
    CLEAN,
    INSUFFICIENT_DATA as QUALITY_INSUFFICIENT,
    PAIR_CHANGED,
    SUSPICIOUS,
    analyze_token_snapshots,
)
from app.services.opportunity_scan import assessment_is_stale, latest_opportunity
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, LOW_CONFIDENCE, SCORED, classify_opportunity
from app.services.paper_money import ZERO, money, money_text, quantity
from app.services.paper_quote_service import (
    FRESHNESS_PROVIDER,
    STATUS_ACCEPTED,
    STATUS_PROVIDER_UNAVAILABLE,
    paper_quote_is_executable,
)
from app.services.paper_quotes import QuoteRejected, load_executable_quote
from app.services.paper_trading import (
    _daily_realized_loss,
    _open_positions,
    _positions,
    ensure_default_portfolio,
    record_decision,
)
from app.services.risk_scoring import classify_measurements
from app.timeutil import as_utc, utc_now

BUY_CANDIDATE = "BUY_CANDIDATE"
WAIT = "WAIT"
REJECT = "REJECT"
DECISION_TYPE = "OPPORTUNITY_EVALUATE"
HARD_QUOTE_REJECTS = {
    "PAIR_MISMATCH",
    "PAIR_CHANGED",
    "SUSPICIOUS_DISCONTINUITY",
    "INVALID_PRICE",
    "INSUFFICIENT_LIQUIDITY",
}
HARD_RISK_BANDS = {"high", "critical"}


class DecisionRequestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class GateResult:
    name: str
    verdict: str
    code: str
    detail: str
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evaluation:
    result: str
    reason: str
    explanation: str
    gates: list[GateResult]
    evidence: dict[str, Any]
    token: Token
    opportunity: OpportunityAssessment | None = None
    security: TokenRiskAssessment | None = None
    quote: PaperMarketQuote | None = None
    snapshot: MarketSnapshot | None = None
    duplicate_of: PaperOpportunityDecision | None = None


@dataclass
class DecisionScanSummary:
    status: str
    scan_id: int | None = None
    tokens_considered: int = 0
    tokens_evaluated: int = 0
    skipped: int = 0
    buy_candidate: int = 0
    wait: int = 0
    reject: int = 0
    orders_created: int = 0
    automatic_execution: bool = False
    external_provider_calls: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    note: str = (
        "Stored-evidence evaluation only. No DexScreener, Solana RPC, or automatic orders."
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        stamp = as_utc(value)
        return stamp.isoformat() if stamp else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _gate(name: str, verdict: str, code: str, detail: str) -> GateResult:
    return GateResult(name=name, verdict=verdict, code=code, detail=detail, passed=verdict == "PASS")


def _combine(gates: list[GateResult]) -> tuple[str, str, str]:
    rejects = [gate for gate in gates if gate.verdict == REJECT]
    waits = [gate for gate in gates if gate.verdict == WAIT]
    if rejects:
        lead = rejects[0]
        extra = ", ".join(item.code for item in rejects[1:])
        explanation = lead.detail if not extra else f"{lead.detail} Additional hard rejects: {extra}."
        return REJECT, lead.code, explanation
    if waits:
        lead = waits[0]
        extra = ", ".join(item.code for item in waits[1:])
        explanation = lead.detail if not extra else f"{lead.detail} Additional waits: {extra}."
        return WAIT, lead.code, explanation
    return (
        BUY_CANDIDATE,
        "ALL_GATES_PASSED",
        "Every mandatory gate passed on stored evidence. This is not an order and not a live trade.",
    )


def _latest_quote(session: Session, token_id: int) -> PaperMarketQuote | None:
    return session.scalar(
        select(PaperMarketQuote)
        .where(PaperMarketQuote.token_id == token_id)
        .order_by(PaperMarketQuote.retrieved_at.desc(), PaperMarketQuote.id.desc())
        .limit(1)
    )


def _latest_snapshot(session: Session, token_id: int) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.token_id == token_id)
        .order_by(MarketSnapshot.recorded_at.desc(), MarketSnapshot.id.desc())
        .limit(1)
    )


def _latest_security(session: Session, token_id: int) -> TokenRiskAssessment | None:
    return session.scalar(
        select(TokenRiskAssessment)
        .where(TokenRiskAssessment.token_id == token_id)
        .order_by(TokenRiskAssessment.scanned_at.desc(), TokenRiskAssessment.id.desc())
        .limit(1)
    )


def _opportunity_gate(
    session: Session,
    token: Token,
    settings: Settings,
    now: datetime,
) -> tuple[GateResult, OpportunityAssessment | None]:
    row = latest_opportunity(session, token.id)
    if row is None:
        return (
            _gate("opportunity", WAIT, "MISSING_OPPORTUNITY", "No stored opportunity assessment."),
            None,
        )
    stale, why, count, last_at = assessment_is_stale(session, token.id, row, now)
    if stale:
        return (
            _gate(
                "opportunity",
                WAIT,
                "OPPORTUNITY_STALE",
                why or "Stored opportunity assessment is not point-in-time valid for current snapshots.",
            ),
            row,
        )
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=row.positive_opportunity_score,
        opportunity_score=row.opportunity_score,
        data_completeness=row.data_completeness,
        confidence=row.confidence,
    )
    if status != SCORED:
        verdict = WAIT
        code = (
            "OPPORTUNITY_LOW_CONFIDENCE"
            if status == LOW_CONFIDENCE or row.confidence < settings.opportunity_min_confidence
            else "OPPORTUNITY_INSUFFICIENT"
        )
        return (
            _gate(
                "opportunity",
                verdict,
                code,
                (
                    f"Opportunity status {status} (stored {row.status}) with completeness "
                    f"{row.data_completeness:.2f} and confidence {row.confidence:.2f}. "
                    f"Floors are completeness {settings.opportunity_min_completeness:.2f} and "
                    f"confidence {settings.opportunity_min_confidence:.2f}."
                ),
            ),
            row,
        )
    if label != HIGH_OPPORTUNITY:
        return (
            _gate(
                "opportunity",
                WAIT,
                "NOT_HIGH_OPPORTUNITY",
                f"Live ranking is {label}, not HIGH_OPPORTUNITY. Score={row.opportunity_score}.",
            ),
            row,
        )
    return (
        _gate(
            "opportunity",
            "PASS",
            "HIGH_OPPORTUNITY",
            (
                f"SCORED HIGH_OPPORTUNITY with completeness {row.data_completeness:.2f} "
                f"and confidence {row.confidence:.2f}. snapshot_count={count}."
            ),
        ),
        row,
    )


def _security_gate(
    row: TokenRiskAssessment | None,
    settings: Settings,
) -> GateResult:
    if row is None:
        return _gate("security", WAIT, "MISSING_SECURITY", "No stored security assessment.")
    classified = classify_measurements(
        settings,
        completeness=row.completeness,
        confidence=row.confidence,
        observed_risk=row.observed_risk,
        observed_risk_band=row.observed_risk_band,
        stored_risk_score=row.risk_score,
        stored_risk_band=row.risk_band,
    )
    band = (classified.risk_band or classified.observed_risk_band or "").lower()
    if classified.classification_reliable and band in HARD_RISK_BANDS:
        return _gate(
            "security",
            REJECT,
            "CONFIRMED_HIGH_RISK",
            (
                f"Reliable security classification is {band} "
                f"(observed_risk={classified.observed_risk})."
            ),
        )
    if not classified.classification_reliable:
        return _gate(
            "security",
            WAIT,
            "UNRELIABLE_SECURITY",
            (
                f"Security is {classified.status}; completeness {row.completeness:.2f} "
                f"and confidence {row.confidence:.2f} do not meet floors "
                f"{settings.security_min_completeness:.2f}/{settings.security_min_confidence:.2f}."
            ),
        )
    return _gate(
        "security",
        "PASS",
        "SECURITY_RELIABLE",
        f"Reliable security band {band or classified.risk_band} is not high/critical.",
    )


def _quality_gate(
    token: Token,
    snapshots: list[MarketSnapshot],
    settings: Settings,
    now: datetime,
) -> GateResult:
    quality = analyze_token_snapshots(
        token.token_address,
        snapshots,
        settings,
        symbol=token.symbol,
        now=now,
    )
    codes = {item.code for item in quality.flags}
    if quality.classification == SUSPICIOUS:
        return _gate(
            "market_quality",
            REJECT,
            "MARKET_QUALITY_SUSPICIOUS",
            quality.explanation,
        )
    if PAIR_CHANGED in codes:
        return _gate(
            "market_quality",
            REJECT,
            "MARKET_PAIR_CHANGED",
            "Stored snapshots show a pair identity change. Pair switching is not ignored.",
        )
    if quality.classification == QUALITY_INSUFFICIENT:
        return _gate(
            "market_quality",
            WAIT,
            "MARKET_QUALITY_INSUFFICIENT",
            quality.explanation,
        )
    if quality.classification != CLEAN:
        return _gate(
            "market_quality",
            WAIT,
            "MARKET_QUALITY_WARNING",
            quality.explanation,
        )
    return _gate("market_quality", "PASS", "MARKET_QUALITY_CLEAN", quality.explanation)


def _quote_gate(
    session: Session,
    token: Token,
    settings: Settings,
    now: datetime,
    stored: PaperMarketQuote | None,
) -> GateResult:
    if stored is not None:
        if stored.status == STATUS_PROVIDER_UNAVAILABLE:
            return _gate("quote", WAIT, "PROVIDER_UNAVAILABLE", stored.notes or "Provider quote unavailable.")
        if stored.reject_reason in HARD_QUOTE_REJECTS:
            return _gate(
                "quote",
                REJECT,
                stored.reject_reason,
                stored.notes or stored.reject_reason,
            )
        if stored.reject_reason in {"MISSING_PRICE", "MISSING_LIQUIDITY"}:
            return _gate(
                "quote",
                WAIT,
                stored.reject_reason,
                stored.notes or stored.reject_reason,
            )
        if stored.status == STATUS_ACCEPTED and stored.freshness_basis != FRESHNESS_PROVIDER:
            return _gate(
                "quote",
                WAIT,
                "UNPROVEN_QUOTE_FRESHNESS",
                "Retrieval time does not prove the underlying market price is fresh.",
            )
        if stored.status == STATUS_ACCEPTED and not paper_quote_is_executable(stored, settings, now=now):
            return _gate(
                "quote",
                WAIT,
                "STALE_QUOTE",
                "Proven quote timestamp is older than the 60-second paper freshness limit.",
            )
        if stored.status == STATUS_ACCEPTED and paper_quote_is_executable(stored, settings, now=now):
            return _gate(
                "quote",
                "PASS",
                "FRESH_PROVEN_QUOTE",
                (
                    f"On-demand quote {stored.id} has provider timestamp within "
                    f"{settings.paper_max_quote_age_seconds:.0f}s and liquidity "
                    f"{stored.liquidity_usd}."
                ),
            )
    try:
        quote = load_executable_quote(session, token, settings, now=now)
    except QuoteRejected as exc:
        verdict = REJECT if exc.reason in HARD_QUOTE_REJECTS else WAIT
        return _gate("quote", verdict, exc.reason, exc.message)
    return _gate(
        "quote",
        "PASS",
        "FRESH_PROVEN_QUOTE",
        f"Executable quote source={quote.source} age={quote.age_seconds:.1f}s.",
    )


def _portfolio_gate(
    session: Session,
    settings: Settings,
    portfolio: PaperPortfolio,
    token: Token,
    now: datetime,
) -> GateResult:
    cash = money(portfolio.cash_available_usd)
    starting = money(portfolio.starting_cash_usd)
    positions = _positions(session, portfolio.id)
    open_rows = _open_positions(positions)
    existing = next((row for row in positions if row.mint_address == token.token_address), None)
    held = quantity(existing.quantity) if existing is not None else ZERO
    max_alloc = money(starting * money(settings.paper_max_allocation_pct))
    max_exposure = money(starting * money(settings.paper_max_exposure_pct))
    invested = sum((money(row.cost_basis_usd) for row in open_rows), ZERO)
    loss = _daily_realized_loss(session, portfolio.id, now)
    max_loss = money(settings.paper_max_daily_realized_loss_usd)
    planned = money(min(cash, max_alloc))

    constraints = {
        "cash_available_usd": money_text(cash),
        "starting_cash_usd": money_text(starting),
        "planned_notional_usd": money_text(planned),
        "max_allocation_usd": money_text(max_alloc),
        "max_exposure_usd": money_text(max_exposure),
        "invested_cost_usd": money_text(invested),
        "open_positions": len(open_rows),
        "max_open_positions": int(settings.paper_max_open_positions),
        "daily_realized_loss_usd": money_text(loss),
        "max_daily_realized_loss_usd": money_text(max_loss),
    }
    if cash <= ZERO:
        return _gate("portfolio", REJECT, "INSUFFICIENT_CASH", json.dumps(constraints))
    if loss <= -max_loss and max_loss > ZERO:
        return _gate("portfolio", REJECT, "MAX_DAILY_LOSS", json.dumps(constraints))
    if (existing is None or held <= ZERO) and len(open_rows) >= int(settings.paper_max_open_positions):
        return _gate("portfolio", REJECT, "MAX_OPEN_POSITIONS", json.dumps(constraints))
    existing_cost = money(existing.cost_basis_usd) if existing is not None and held > ZERO else ZERO
    if existing_cost + planned > max_alloc:
        return _gate("portfolio", REJECT, "MAX_ALLOCATION", json.dumps(constraints))
    projected = invested + planned
    if projected > max_exposure:
        return _gate("portfolio", REJECT, "MAX_EXPOSURE", json.dumps(constraints))
    return _gate(
        "portfolio",
        "PASS",
        "PORTFOLIO_WITHIN_LIMITS",
        (
            f"Cash {money_text(cash)} can fund planned {money_text(planned)} within "
            f"allocation {money_text(max_alloc)} and exposure {money_text(max_exposure)}."
        ),
    )


def evaluate_token(
    session: Session,
    token: Token,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    portfolio: PaperPortfolio | None = None,
) -> Evaluation:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    portfolio = portfolio or ensure_default_portfolio(session, settings)
    snapshots = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token.id)
            .order_by(MarketSnapshot.recorded_at.asc(), MarketSnapshot.id.asc())
        ).all()
    )
    stored_quote = _latest_quote(session, token.id)
    security = _latest_security(session, token.id)
    opp_gate, opportunity = _opportunity_gate(session, token, settings, now)
    gates = [
        opp_gate,
        _security_gate(security, settings),
        _quality_gate(token, snapshots, settings, now),
        _quote_gate(session, token, settings, now, stored_quote),
        _portfolio_gate(session, settings, portfolio, token, now),
    ]
    result, reason, explanation = _combine(gates)
    snapshot = _latest_snapshot(session, token.id)
    evidence = {
        "token_id": token.id,
        "mint_address": token.token_address,
        "pair_address": token.pair_address,
        "decided_at": now,
        "opportunity": None
        if opportunity is None
        else {
            "id": opportunity.id,
            "assessed_at": opportunity.assessed_at,
            "status": opportunity.status,
            "ranking_label": opportunity.ranking_label,
            "opportunity_score": opportunity.opportunity_score,
            "confidence": opportunity.confidence,
            "data_completeness": opportunity.data_completeness,
            "snapshot_count": opportunity.snapshot_count,
            "last_snapshot_at": opportunity.last_snapshot_at,
        },
        "security": None
        if security is None
        else {
            "id": security.id,
            "scanned_at": security.scanned_at,
            "status": security.status,
            "observed_risk": security.observed_risk,
            "observed_risk_band": security.observed_risk_band,
            "risk_score": security.risk_score,
            "risk_band": security.risk_band,
            "confidence": security.confidence,
            "completeness": security.completeness,
        },
        "quote": None
        if stored_quote is None
        else {
            "id": stored_quote.id,
            "status": stored_quote.status,
            "freshness_basis": stored_quote.freshness_basis,
            "retrieved_at": stored_quote.retrieved_at,
            "market_observed_at": stored_quote.market_observed_at,
            "price_usd": stored_quote.price_usd,
            "liquidity_usd": stored_quote.liquidity_usd,
            "pair_address": stored_quote.pair_address,
            "reject_reason": stored_quote.reject_reason,
            "discontinuity_flag": stored_quote.discontinuity_flag,
            "source": stored_quote.source,
        },
        "snapshot": None
        if snapshot is None
        else {
            "id": snapshot.id,
            "recorded_at": snapshot.recorded_at,
            "price_usd": snapshot.price_usd,
            "liquidity_usd": snapshot.liquidity_usd,
        },
        "portfolio": {
            "id": portfolio.id,
            "cash_available_usd": portfolio.cash_available_usd,
            "mode": portfolio.mode,
        },
        "thresholds": {
            "opportunity_min_completeness": settings.opportunity_min_completeness,
            "opportunity_min_confidence": settings.opportunity_min_confidence,
            "opportunity_high_threshold": settings.opportunity_high_threshold,
            "security_min_completeness": settings.security_min_completeness,
            "security_min_confidence": settings.security_min_confidence,
            "paper_max_quote_age_seconds": settings.paper_max_quote_age_seconds,
            "paper_min_liquidity_usd": settings.paper_min_liquidity_usd,
        },
        "automatic_execution": False,
        "orders_created": 0,
        "external_provider_calls": 0,
        "precedence": "Any confirmed REJECT overrides WAIT from missing evidence.",
    }
    return Evaluation(
        result=result,
        reason=reason,
        explanation=explanation,
        gates=gates,
        evidence=evidence,
        token=token,
        opportunity=opportunity,
        security=security,
        quote=stored_quote,
        snapshot=snapshot,
    )


def _recent_decision(
    session: Session,
    token_id: int,
    settings: Settings,
    now: datetime,
) -> PaperOpportunityDecision | None:
    seconds = int(settings.paper_decision_rescan_seconds)
    if seconds <= 0:
        return None
    cutoff = now - timedelta(seconds=seconds)
    return session.scalar(
        select(PaperOpportunityDecision)
        .where(
            PaperOpportunityDecision.token_id == token_id,
            PaperOpportunityDecision.decided_at >= cutoff,
            PaperOpportunityDecision.duplicate_of_id.is_(None),
        )
        .order_by(PaperOpportunityDecision.decided_at.desc(), PaperOpportunityDecision.id.desc())
        .limit(1)
    )


def persist_evaluation(
    session: Session,
    evaluation: Evaluation,
    settings: Settings,
    *,
    scan_id: int | None = None,
    now: datetime | None = None,
) -> PaperOpportunityDecision:
    now = as_utc(now or utc_now())
    portfolio = ensure_default_portfolio(session, settings)
    result = evaluation.result
    reason = evaluation.reason
    explanation = evaluation.explanation
    gates = [gate.as_dict() for gate in evaluation.gates]
    evidence = _json_safe(evaluation.evidence)
    paper_row = record_decision(
        session,
        portfolio,
        decision_type=DECISION_TYPE,
        outcome=result,
        reason=reason[:64],
        mint=evaluation.token.token_address,
        token_id=evaluation.token.id,
        details={
            "result": result,
            "reason": reason,
            "explanation": explanation,
            "gates": gates,
            "evidence": evidence,
            "orders_created": 0,
            "automatic_execution": False,
            "duplicate": False,
        },
    )
    session.flush()
    row = PaperOpportunityDecision(
        paper_decision_id=paper_row.id,
        portfolio_id=portfolio.id,
        token_id=evaluation.token.id,
        mint_address=evaluation.token.token_address,
        decided_at=now,
        result=result,
        reason=reason[:64],
        explanation=explanation,
        opportunity_assessment_id=evaluation.opportunity.id if evaluation.opportunity else None,
        security_assessment_id=evaluation.security.id if evaluation.security else None,
        paper_quote_id=evaluation.quote.id if evaluation.quote else None,
        snapshot_id=evaluation.snapshot.id if evaluation.snapshot else None,
        scan_id=scan_id,
        opportunity_confidence=evaluation.opportunity.confidence if evaluation.opportunity else None,
        opportunity_completeness=evaluation.opportunity.data_completeness if evaluation.opportunity else None,
        security_confidence=evaluation.security.confidence if evaluation.security else None,
        security_completeness=evaluation.security.completeness if evaluation.security else None,
        gates_json=json.dumps(gates),
        evidence_json=json.dumps(evidence),
        orders_created=0,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def load_catalog_token(session: Session, mint: str) -> Token:
    mint = mint.strip()
    token = session.scalar(select(Token).where(Token.token_address == mint))
    if token is None:
        raise DecisionRequestError(404, "Token is not in the stored catalog")
    if str(token.chain or "").lower() != "solana":
        raise DecisionRequestError(422, "Token is not recorded as a Solana mint")
    return token


def evaluate_and_store(
    session: Session,
    mint: str,
    settings: Settings | None = None,
    *,
    force: bool = False,
    scan_id: int | None = None,
    now: datetime | None = None,
) -> tuple[PaperOpportunityDecision, bool]:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    token = load_catalog_token(session, mint)
    if not force:
        recent = _recent_decision(session, token.id, settings, now)
        if recent is not None:
            return recent, True
    evaluation = evaluate_token(session, token, settings, now=now)
    return persist_evaluation(session, evaluation, settings, scan_id=scan_id, now=now), False


def scan_decisions(
    session: Session,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    force: bool = False,
    token_addresses: list[str] | None = None,
    now: datetime | None = None,
) -> DecisionScanSummary:
    settings = settings or get_settings()
    now = as_utc(now or utc_now())
    ensure_default_portfolio(session, settings)
    cap = limit if limit is not None else int(settings.paper_decision_scan_limit)
    cap = max(1, min(cap, 200))
    started = now
    scan_row = IntelligenceScan(
        scan_type="paper_opportunity",
        started_at=started,
        status="running",
    )
    session.add(scan_row)
    session.commit()
    summary = DecisionScanSummary(status="running", scan_id=scan_row.id)
    if token_addresses:
        tokens: list[Token] = []
        for address in token_addresses[:cap]:
            row = session.scalar(select(Token).where(Token.token_address == address.strip()))
            if row is not None:
                tokens.append(row)
    else:
        tokens = list(
            session.scalars(
                select(Token)
                .where(Token.chain == "solana")
                .order_by(Token.last_discovered_at.desc(), Token.id.desc())
                .limit(cap)
            ).all()
        )
    summary.tokens_considered = len(tokens)
    for token in tokens:
        try:
            row, skipped = evaluate_and_store(
                session,
                token.token_address,
                settings,
                force=force,
                scan_id=scan_row.id,
                now=now,
            )
            if skipped:
                summary.skipped += 1
            else:
                summary.tokens_evaluated += 1
            if row.result == BUY_CANDIDATE:
                summary.buy_candidate += 1
            elif row.result == WAIT:
                summary.wait += 1
            elif row.result == REJECT:
                summary.reject += 1
            summary.decisions.append(
                {
                    "id": row.id,
                    "paper_decision_id": row.paper_decision_id,
                    "mint_address": row.mint_address,
                    "result": row.result,
                    "reason": row.reason,
                    "duplicate": skipped,
                }
            )
        except Exception as exc:  # noqa: BLE001
            summary.errors.append({"token_address": token.token_address, "detail": str(exc)})
            session.rollback()
    summary.status = "completed"
    scan_row.finished_at = utc_now()
    scan_row.status = summary.status
    scan_row.items_fetched = summary.tokens_considered
    scan_row.items_saved = summary.tokens_evaluated
    scan_row.duplicates_skipped = summary.skipped
    scan_row.details_json = json.dumps(
        {
            "buy_candidate": summary.buy_candidate,
            "wait": summary.wait,
            "reject": summary.reject,
            "orders_created": 0,
            "automatic_execution": False,
            "external_provider_calls": 0,
        }
    )
    scan_row.errors_json = json.dumps(summary.errors)
    session.commit()
    return summary


def decision_public(row: PaperOpportunityDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "paper_decision_id": row.paper_decision_id,
        "token_id": row.token_id,
        "mint_address": row.mint_address,
        "decided_at": row.decided_at,
        "result": row.result,
        "reason": row.reason,
        "explanation": row.explanation,
        "gates": json.loads(row.gates_json),
        "evidence": json.loads(row.evidence_json),
        "opportunity_assessment_id": row.opportunity_assessment_id,
        "security_assessment_id": row.security_assessment_id,
        "paper_quote_id": row.paper_quote_id,
        "snapshot_id": row.snapshot_id,
        "scan_id": row.scan_id,
        "opportunity_confidence": row.opportunity_confidence,
        "opportunity_completeness": row.opportunity_completeness,
        "security_confidence": row.security_confidence,
        "security_completeness": row.security_completeness,
        "duplicate_of_id": row.duplicate_of_id,
        "orders_created": row.orders_created,
        "automatic_execution": False,
        "note": "Paper opportunity decision. Not an order, wallet action, or chain transaction.",
    }


def get_opportunity_decision(session: Session, decision_id: int) -> PaperOpportunityDecision:
    row = session.get(PaperOpportunityDecision, decision_id)
    if row is None:
        paper = session.get(PaperDecision, decision_id)
        if paper is not None:
            row = session.scalar(
                select(PaperOpportunityDecision).where(
                    PaperOpportunityDecision.paper_decision_id == paper.id
                )
            )
    if row is None:
        raise DecisionRequestError(404, "Opportunity decision not found")
    return row
