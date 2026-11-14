"""HTTP tests for security endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import get_session_factory
from app.models import (
    HolderAnalysis,
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    OpportunityAssessment,
    RiskScoreHistory,
    Token,
    TokenRiskAssessment,
    TokenSecurityScan,
    WalletActivity,
    utc_now,
)
from app.services.security_scan import SecurityScanSummary
from tests.dexscreener_fakes import TOKEN_A, TOKEN_B


def _clean() -> None:
    session = get_session_factory()()
    session.execute(delete(RiskScoreHistory))
    session.execute(delete(TokenRiskAssessment))
    session.execute(delete(WalletActivity))
    session.execute(delete(LiquidityAnalysisRecord))
    session.execute(delete(HolderAnalysis))
    session.execute(delete(TokenSecurityScan))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(IntelligenceScan))
    session.commit()
    session.close()


def test_security_api_flow(client: TestClient, monkeypatch) -> None:
    _clean()
    session = get_session_factory()()
    token = Token(token_address=TOKEN_A, name="Test", symbol="TST", chain="solana")
    weak = Token(token_address=TOKEN_B, name="Weak", symbol="WEAK", chain="solana")
    session.add_all([token, weak])
    session.commit()
    session.add(
        TokenRiskAssessment(
            token_id=token.id,
            scanned_at=utc_now(),
            status="scored",
            observed_risk=82,
            observed_risk_band="critical",
            risk_score=82,
            risk_band="critical",
            confidence=0.8,
            completeness=0.8,
            category_scores_json="[]",
            warnings_json='["mint authority active"]',
            explanation="Observed risk 82/100.",
        )
    )
    session.add(
        TokenRiskAssessment(
            token_id=weak.id,
            scanned_at=utc_now(),
            status="INSUFFICIENT_DATA",
            observed_risk=75,
            observed_risk_band="high",
            risk_score=None,
            risk_band=None,
            confidence=0.36,
            completeness=0.40,
            category_scores_json='[{"name":"liquidity_risk","weight":0.25,"score":40,"status":"ESTIMATE","reason":"liq"}]',
            warnings_json="[]",
            explanation="INSUFFICIENT_DATA",
        )
    )
    session.add(
        RiskScoreHistory(
            token_id=token.id,
            scanned_at=utc_now(),
            status="scored",
            observed_risk=82,
            observed_risk_band="critical",
            risk_score=82,
            risk_band="critical",
            confidence=0.8,
            completeness=0.8,
            category_scores_json="[]",
            explanation="Observed risk 82/100.",
        )
    )
    session.commit()
    token_id = token.id
    session.close()

    async def fake_scan(*args, **kwargs):
        return SecurityScanSummary(
            status="completed",
            tokens_considered=1,
            tokens_scanned=1,
            skipped=0,
            insufficient_data=0,
            high_risk=1,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("app.api.security.scan_tokens", fake_scan)
    monkeypatch.setattr("app.api.security.SolanaRpcClient", lambda **kwargs: _DummyRpc())

    scanned = client.post("/api/v1/security/scan", json={"limit": 5, "force": True})
    assert scanned.status_code == 200
    assert scanned.json()["tokens_scanned"] == 1

    one = client.post(f"/api/v1/security/scan/{TOKEN_A}?force=true")
    assert one.status_code == 200

    missing = client.post("/api/v1/security/scan/DoesNotExist111")
    assert missing.status_code == 404

    detail = client.get(f"/api/v1/security/tokens/{TOKEN_A}")
    assert detail.status_code == 200
    assert detail.json()["assessment"]["risk_score"] == 82
    assert detail.json()["assessment"]["observed_risk"] == 82
    assert detail.json()["assessment"]["classification_reliable"] is True
    assert "guarantee" in detail.json()["safety_note"].lower()

    history = client.get(f"/api/v1/security/tokens/{TOKEN_A}/history")
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1

    high = client.get("/api/v1/security/high-risk")
    assert high.status_code == 200
    assert high.json()["total"] == 1
    assert high.json()["items"][0]["token_address"] == TOKEN_A
    assert high.json()["provisional_total"] == 1
    assert high.json()["provisional"][0]["token_address"] == TOKEN_B
    assert high.json()["provisional"][0]["assessment"]["status"] == "INSUFFICIENT_DATA"
    assert high.json()["provisional"][0]["assessment"]["classification_reliable"] is False

    summary = client.get("/api/v1/security/summary")
    assert summary.status_code == 200
    assert summary.json()["high_or_critical"] == 1
    assert summary.json()["insufficient_data"] == 1

    status_payload = client.get("/api/v1/security/status")
    assert status_payload.status_code == 200
    assert status_payload.json()["solana_rpc_configured"] is True
    _ = token_id


def test_high_risk_does_not_confirm_legacy_unreliable_records(client: TestClient) -> None:
    _clean()
    session = get_session_factory()()
    nnn = Token(
        token_address="57YQJbgdE32ZMCQzBivhjcXmnMLY5iw2MwrtcPYWpump",
        name="NNN",
        symbol="NNN",
        chain="solana",
    )
    session.add(nnn)
    session.commit()
    categories = (
        '[{"name":"token_authorities","weight":0.20,"score":null,"status":"UNKNOWN","reason":"unavailable"},'
        '{"name":"holder_concentration","weight":0.20,"score":null,"status":"UNKNOWN","reason":"unavailable"},'
        '{"name":"liquidity_risk","weight":0.25,"score":40,"status":"ESTIMATE","reason":"liq"},'
        '{"name":"trading_behavior","weight":0.15,"score":70,"status":"ESTIMATE","reason":"trading"},'
        '{"name":"developer_activity","weight":0.20,"score":null,"status":"UNKNOWN","reason":"unavailable"}]'
    )
    session.add(
        TokenRiskAssessment(
            token_id=nnn.id,
            scanned_at=utc_now(),
            status="scored",
            observed_risk=None,
            observed_risk_band=None,
            risk_score=75,
            risk_band="high",
            confidence=0.364,
            completeness=0.4,
            category_scores_json=categories,
            warnings_json="[]",
            explanation="Final assessment scored. Observed risk 75/100.",
        )
    )
    session.add(
        RiskScoreHistory(
            token_id=nnn.id,
            scanned_at=utc_now(),
            status="scored",
            observed_risk=None,
            observed_risk_band=None,
            risk_score=75,
            risk_band="high",
            confidence=0.364,
            completeness=0.4,
            category_scores_json=categories,
            explanation="Final assessment scored. Observed risk 75/100.",
        )
    )
    session.commit()
    session.close()

    detail = client.get("/api/v1/security/tokens/57YQJbgdE32ZMCQzBivhjcXmnMLY5iw2MwrtcPYWpump")
    assert detail.status_code == 200
    assessment = detail.json()["assessment"]
    assert assessment["status"] == "INSUFFICIENT_DATA"
    assert assessment["observed_risk"] == 75
    assert assessment["observed_risk_band"] == "high"
    assert assessment["risk_score"] is None
    assert assessment["risk_band"] is None
    assert assessment["confidence"] == pytest.approx(0.364)
    assert assessment["completeness"] == pytest.approx(0.4)
    assert assessment["classification_reliable"] is False
    assert set(assessment["unknown_categories"]) == {
        "token_authorities",
        "holder_concentration",
        "developer_activity",
    }

    history = client.get("/api/v1/security/tokens/57YQJbgdE32ZMCQzBivhjcXmnMLY5iw2MwrtcPYWpump/history")
    assert history.json()["items"][0]["status"] == "INSUFFICIENT_DATA"
    assert history.json()["items"][0]["risk_score"] is None
    assert history.json()["items"][0]["observed_risk"] == 75

    high = client.get("/api/v1/security/high-risk")
    assert high.status_code == 200
    assert high.json()["total"] == 0
    assert high.json()["items"] == []
    assert high.json()["provisional_total"] == 1
    assert high.json()["provisional"][0]["token_address"] == "57YQJbgdE32ZMCQzBivhjcXmnMLY5iw2MwrtcPYWpump"
    assert high.json()["provisional"][0]["assessment"]["status"] == "INSUFFICIENT_DATA"
    assert high.json()["provisional"][0]["assessment"]["classification_reliable"] is False

    summary = client.get("/api/v1/security/summary")
    assert summary.json()["high_or_critical"] == 0
    assert summary.json()["insufficient_data"] == 1

    session = get_session_factory()()
    stored = session.scalar(select(TokenRiskAssessment))
    assert stored is not None
    assert stored.status == "scored"
    assert stored.risk_score == 75
    assert stored.observed_risk is None
    session.close()


class _DummyRpc:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None
