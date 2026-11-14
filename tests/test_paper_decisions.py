"""Phase 10B.2 stored-evidence opportunity decisions. No DexScreener, RPC, or orders."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    IntelligenceScan,
    MarketSnapshot,
    OpportunityAssessment,
    PaperAutoEntryAttempt,
    PaperAutoEntryCycle,
    PaperAutoExitAttempt,
    PaperAutoExitCycle,
    PaperDecision,
    PaperEquitySnapshot,
    PaperMarketQuote,
    PaperOpportunityDecision,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperPositionObservation,
    PaperTrade,
    Token,
    TokenRiskAssessment,
)
from app.services.paper_decision_engine import (
    BUY_CANDIDATE,
    REJECT,
    WAIT,
    evaluate_and_store,
    evaluate_token,
    get_opportunity_decision,
    scan_decisions,
)
from app.services.paper_trading import ensure_default_portfolio
from app.timeutil import utc_now

MINT_BUY = "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump"
MINT_WAIT = "HoqJaoes6yfL8YEsHGfENwVKT7LrAPy97ZDg9hmBu4Ep"
MINT_REJECT = "So11111111111111111111111111111111111111112"
PAIR = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _wipe(session) -> None:
    session.execute(delete(PaperAutoExitAttempt))
    session.execute(delete(PaperAutoExitCycle))
    session.execute(delete(PaperPositionObservation))
    session.execute(delete(PaperAutoEntryAttempt))
    session.execute(delete(PaperAutoEntryCycle))
    session.execute(delete(PaperOpportunityDecision))
    session.execute(delete(PaperTrade))
    session.execute(delete(PaperOrder))
    session.execute(delete(PaperDecision))
    session.execute(delete(PaperEquitySnapshot))
    session.execute(delete(PaperPosition))
    session.execute(delete(PaperPortfolio))
    session.execute(delete(PaperMarketQuote))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(TokenRiskAssessment))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(IntelligenceScan))
    session.execute(delete(Token))
    session.commit()


def _forbid_providers(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("external provider called during decision evaluation")

    monkeypatch.setattr("app.services.dexscreener_client.DexScreenerClient.get_token_pairs", boom)
    monkeypatch.setattr("app.services.solana_rpc.SolanaRpcClient.rpc", boom)


def _token(session, mint: str, symbol: str = "T") -> Token:
    row = Token(
        token_address=mint,
        symbol=symbol,
        name=symbol,
        chain="solana",
        pair_address=PAIR,
        last_discovered_at=utc_now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _snapshots(session, token: Token) -> list[MarketSnapshot]:
    now = utc_now()
    rows = []
    for index, age in enumerate((40, 10)):
        snap = MarketSnapshot(
            token_id=token.id,
            price_usd=1.0 + index * 0.01,
            liquidity_usd=25000,
            market_cap=20000 + index * 100,
            fdv=22000 + index * 100,
            pair_address=PAIR,
            recorded_at=now - timedelta(seconds=age),
            discovery_source="test",
        )
        session.add(snap)
        rows.append(snap)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def _opportunity(session, token: Token, snaps: list[MarketSnapshot], **overrides) -> OpportunityAssessment:
    payload = {
        "token_id": token.id,
        "assessed_at": utc_now(),
        "status": "SCORED",
        "ranking_label": "HIGH_OPPORTUNITY",
        "opportunity_score": 80.0,
        "positive_opportunity_score": 82.0,
        "security_adjustment": 0.0,
        "confidence": 0.80,
        "data_completeness": 0.80,
        "explanation": "test opportunity",
        "snapshot_count": len(snaps),
        "last_snapshot_at": snaps[-1].recorded_at,
        "security_coverage": "CONFIRMED",
    }
    payload.update(overrides)
    row = OpportunityAssessment(**payload)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _security(session, token: Token, **overrides) -> TokenRiskAssessment:
    payload = {
        "token_id": token.id,
        "scanned_at": utc_now(),
        "status": "scored",
        "observed_risk": 20.0,
        "observed_risk_band": "moderate_low",
        "risk_score": 20.0,
        "risk_band": "moderate_low",
        "confidence": 1.0,
        "completeness": 1.0,
        "explanation": "test security",
    }
    payload.update(overrides)
    row = TokenRiskAssessment(**payload)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _quote(session, token: Token, **overrides) -> PaperMarketQuote:
    now = utc_now()
    payload = {
        "token_id": token.id,
        "mint_address": token.token_address,
        "pair_address": PAIR,
        "price_usd": 1.01,
        "liquidity_usd": 25000.0,
        "source": "dexscreener_on_demand",
        "retrieved_at": now,
        "market_observed_at": now - timedelta(seconds=5),
        "freshness_basis": "PROVIDER_TIMESTAMP",
        "status": "ACCEPTED",
        "pair_match_status": "TRACKED_PAIR",
        "cache_hit": 0,
    }
    payload.update(overrides)
    row = PaperMarketQuote(**payload)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _buy_ready(session, mint: str, symbol: str = "BUY") -> Token:
    token = _token(session, mint, symbol)
    snaps = _snapshots(session, token)
    _opportunity(session, token, snaps)
    _security(session, token)
    _quote(session, token)
    return token


def _gate(evaluation, name: str) -> dict:
    return next(item.as_dict() for item in evaluation.gates if item.name == name)


def test_fully_qualified_buy_candidate(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == BUY_CANDIDATE
        assert all(gate.verdict == "PASS" for gate in evaluation.gates)
        row, skipped = evaluate_and_store(session, MINT_BUY, get_settings(), force=True)
        assert skipped is False
        assert row.result == BUY_CANDIDATE
        assert row.orders_created == 0
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        loaded = get_opportunity_decision(session, row.id)
        assert json_gates(loaded) == [gate.as_dict() for gate in evaluation.gates]
    finally:
        session.close()


def json_gates(row: PaperOpportunityDecision) -> list:
    import json

    return json.loads(row.gates_json)


def test_missing_or_unreliable_security_is_wait(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session, MINT_WAIT, "WAIT")
        snaps = _snapshots(session, token)
        _opportunity(session, token, snaps)
        _quote(session, token)
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == WAIT
        assert _gate(evaluation, "security")["code"] == "MISSING_SECURITY"
        _security(session, token, completeness=0.2, confidence=0.2, status="INSUFFICIENT_DATA")
        again = evaluate_token(session, token, get_settings())
        assert again.result == WAIT
        assert _gate(again, "security")["code"] == "UNRELIABLE_SECURITY"
        assert _gate(again, "security")["verdict"] == WAIT
    finally:
        session.close()


def test_low_confidence_opportunity_is_wait(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session, MINT_BUY, "LOW")
        snaps = _snapshots(session, token)
        _opportunity(
            session,
            token,
            snaps,
            status="LOW_CONFIDENCE",
            ranking_label="HIGH_OPPORTUNITY",
            confidence=0.20,
            data_completeness=0.80,
        )
        _security(session, token)
        _quote(session, token)
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == WAIT
        assert _gate(evaluation, "opportunity")["code"] == "OPPORTUNITY_LOW_CONFIDENCE"
    finally:
        session.close()


def test_stale_and_unproven_quotes_wait(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(session, token, market_observed_at=utc_now() - timedelta(seconds=120))
        stale = evaluate_token(session, token, get_settings())
        assert stale.result == WAIT
        assert _gate(stale, "quote")["code"] == "STALE_QUOTE"
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(session, token, freshness_basis="RETRIEVAL_UNPROVEN", market_observed_at=None)
        unproven = evaluate_token(session, token, get_settings())
        assert unproven.result == WAIT
        assert _gate(unproven, "quote")["code"] == "UNPROVEN_QUOTE_FRESHNESS"
    finally:
        session.close()


def test_missing_liquidity_waits(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(
            session,
            token,
            status="REJECTED",
            reject_reason="MISSING_LIQUIDITY",
            liquidity_usd=None,
            notes="Liquidity is missing.",
        )
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == WAIT
        assert _gate(evaluation, "quote")["code"] == "MISSING_LIQUIDITY"
        assert evaluation.quote is not None
        assert evaluation.quote.liquidity_usd is None
    finally:
        session.close()


def test_pair_mismatch_and_discontinuity_reject(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(session, token, status="REJECTED", reject_reason="PAIR_MISMATCH", notes="Tracked pair missing.")
        mismatch = evaluate_token(session, token, get_settings())
        assert mismatch.result == REJECT
        assert _gate(mismatch, "quote")["code"] == "PAIR_MISMATCH"
        session.execute(delete(PaperMarketQuote))
        session.commit()
        _quote(
            session,
            token,
            status="REJECTED",
            reject_reason="SUSPICIOUS_DISCONTINUITY",
            discontinuity_flag="SUSPICIOUS_DISCONTINUITY",
            notes="Jump exceeded multiple.",
        )
        jump = evaluate_token(session, token, get_settings())
        assert jump.result == REJECT
        assert _gate(jump, "quote")["code"] == "SUSPICIOUS_DISCONTINUITY"
    finally:
        session.close()


def test_confirmed_high_risk_rejects_even_if_quote_missing(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _token(session, MINT_REJECT, "RISK")
        snaps = _snapshots(session, token)
        _opportunity(session, token, snaps)
        _security(
            session,
            token,
            observed_risk=88.0,
            observed_risk_band="critical",
            risk_score=88.0,
            risk_band="critical",
        )
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == REJECT
        assert _gate(evaluation, "security")["code"] == "CONFIRMED_HIGH_RISK"
        assert _gate(evaluation, "security")["verdict"] == REJECT
        assert any(gate.verdict == "PASS" for gate in evaluation.gates)
    finally:
        session.close()


def test_portfolio_exposure_restriction_rejects(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        portfolio = ensure_default_portfolio(session, get_settings())
        portfolio.cash_available_usd = "0"
        session.commit()
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == REJECT
        assert _gate(evaluation, "portfolio")["code"] == "INSUFFICIENT_CASH"
    finally:
        session.close()


def test_duplicate_scan_skips_recent_and_creates_no_orders(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
        first = scan_decisions(session, get_settings(), token_addresses=[MINT_BUY], force=True)
        assert first.buy_candidate == 1
        assert first.orders_created == 0
        second = scan_decisions(session, get_settings(), token_addresses=[MINT_BUY], force=False)
        assert second.skipped == 1
        assert second.tokens_evaluated == 0
        assert session.scalar(select(func.count()).select_from(PaperOpportunityDecision)) == 1
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
    finally:
        session.close()


def test_reproducible_records_and_list_api(client: TestClient, monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY)
    finally:
        session.close()
    first = client.post(f"/api/v1/paper/decisions/evaluate/{MINT_BUY}?force=true")
    assert first.status_code == 200
    body = first.json()
    assert body["result"] == BUY_CANDIDATE
    assert body["orders_created"] == 0
    assert body["automatic_execution"] is False
    fetched = client.get(f"/api/v1/paper/decisions/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["gates"] == body["gates"]
    assert fetched.json()["evidence"]["opportunity"]["id"] == body["opportunity_assessment_id"]
    listed = client.get("/api/v1/paper/decisions")
    assert listed.status_code == 200
    outcomes = [item["outcome"] for item in listed.json()["items"]]
    assert BUY_CANDIDATE in outcomes
    listed_id = next(item["id"] for item in listed.json()["items"] if item["outcome"] == BUY_CANDIDATE)
    via_paper = client.get(f"/api/v1/paper/decisions/{listed_id}")
    assert via_paper.status_code == 200
    assert via_paper.json()["result"] == BUY_CANDIDATE


def test_bounded_stored_scan_counts_and_no_provider_calls(client: TestClient, monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        _buy_ready(session, MINT_BUY, "BUY")
        wait_token = _token(session, MINT_WAIT, "WAIT")
        wait_snaps = _snapshots(session, wait_token)
        _opportunity(session, wait_token, wait_snaps)
        _quote(session, wait_token)
        reject_token = _token(session, MINT_REJECT, "REJ")
        reject_snaps = _snapshots(session, reject_token)
        _opportunity(session, reject_token, reject_snaps)
        _security(
            session,
            reject_token,
            observed_risk=90.0,
            observed_risk_band="critical",
            risk_score=90.0,
            risk_band="critical",
        )
        _quote(session, reject_token)
    finally:
        session.close()
    response = client.post(
        "/api/v1/paper/decisions/scan",
        json={"limit": 3, "force": True, "token_addresses": [MINT_BUY, MINT_WAIT, MINT_REJECT]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tokens_considered"] == 3
    assert body["buy_candidate"] == 1
    assert body["wait"] == 1
    assert body["reject"] == 1
    assert body["orders_created"] == 0
    assert body["automatic_execution"] is False
    assert body["external_provider_calls"] == 0
    session = get_session_factory()()
    try:
        assert session.scalar(select(func.count()).select_from(PaperOrder)) == 0
        assert session.scalar(select(func.count()).select_from(PaperOpportunityDecision)) == 3
    finally:
        session.close()


def test_open_position_limit_rejects_new_candidate(monkeypatch) -> None:
    _forbid_providers(monkeypatch)
    session = get_session_factory()()
    try:
        _wipe(session)
        token = _buy_ready(session, MINT_BUY)
        portfolio = ensure_default_portfolio(session, get_settings())
        for mint in ("Pos111111111111111111111111111111111111111", "Pos222222222222222222222222222222222222222", "Pos333333333333333333333333333333333333333"):
            extra = Token(token_address=mint, symbol="P", chain="solana")
            session.add(extra)
            session.flush()
            session.add(
                PaperPosition(
                    portfolio_id=portfolio.id,
                    token_id=extra.id,
                    mint_address=mint,
                    quantity="1",
                    average_cost_usd="1",
                    cost_basis_usd="1",
                    realized_pnl_usd="0",
                    opened_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        session.commit()
        evaluation = evaluate_token(session, token, get_settings())
        assert evaluation.result == REJECT
        assert _gate(evaluation, "portfolio")["code"] == "MAX_OPEN_POSITIONS"
    finally:
        session.close()
