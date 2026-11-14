"""Token risk and security tests with mocked Solana RPC."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    CreatorObservation,
    RpcCooldownState,
    TokenAccountOwnerObservation,
    MetaplexCreatorObservation,
    HolderAnalysis,
    IntelligenceScan,
    LiquidityAnalysisRecord,
    MarketSnapshot,
    OpportunityAssessment,
    RiskScoreHistory,
    Token,
    Token2022ExtensionObservation,
    TokenRiskAssessment,
    TokenSecurityScan,
    WalletActivity,
    utc_now,
)
from app.services.holder_analysis import analyze_holders
from app.services.liquidity_analysis import analyze_liquidity
from app.services.risk_scoring import (
    INSUFFICIENT_DATA,
    LOW_CONFIDENCE,
    classify_measurements,
    score_token_risk,
)
from app.services.security_scan import scan_tokens
from app.services.solana_rpc import SolanaRpcClient, SolanaRpcError
from app.services.token_security import analyze_token_security
from app.services.trading_analysis import analyze_trading
from app.services.wallet_analysis import analyze_wallet_activity
from tests.dexscreener_fakes import TOKEN_A, run

MINT = "Mint111111111111111111111111111111111111111"
FREEZE = "Frz1111111111111111111111111111111111111111"
PAIR = "Pair11111111111111111111111111111111111111"
BURN = "1nc1nerator11111111111111111111111111111111"
HOLDER = "Hold11111111111111111111111111111111111111"


class RpcScript:
    def __init__(self) -> None:
        self.account: dict | None | httpx.Response = None
        self.supply: dict | None | httpx.Response = None
        self.largest: list | httpx.Response = []
        self.calls: list[str] = []
        self._rate_remaining = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        method = payload.get("method")
        self.calls.append(method)
        if self._rate_remaining > 0:
            self._rate_remaining -= 1
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate"})
        if method == "getAccountInfo":
            params = payload.get("params") or []
            address = params[0] if params else None
            options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            encoding = options.get("encoding")
            if hasattr(self, "accounts") and address in getattr(self, "accounts", {}):
                return self._wrap(self.accounts[address])
            if encoding == "base64":
                return self._wrap(getattr(self, "metadata", None))
            return self._wrap(self.account)
        if method == "getTokenSupply":
            return self._wrap(self.supply)
        if method == "getTokenLargestAccounts":
            return self._wrap(self.largest)
        if method == "getSignaturesForAddress":
            pages = getattr(self, "signature_pages", None)
            if pages is not None:
                index = getattr(self, "_sig_page_index", 0)
                self._sig_page_index = index + 1
                if index < len(pages):
                    return self._wrap_raw(pages[index])
                return self._wrap_raw([])
            return self._wrap_raw(getattr(self, "signatures", []))
        if method == "getTransaction":
            params = payload.get("params") or []
            signature = params[0] if params else None
            transactions = getattr(self, "transactions", {})
            if signature in transactions:
                return self._wrap_raw(transactions[signature])
            return self._wrap_raw(getattr(self, "transaction", None))
        return httpx.Response(404, json={"error": "not mocked"})

    def _wrap(self, value) -> httpx.Response:
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 4242}, "value": value}},
        )

    def _wrap_raw(self, value) -> httpx.Response:
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": value})

    def client(self) -> SolanaRpcClient:
        return SolanaRpcClient(
            settings=get_settings(),
            transport=httpx.MockTransport(self.handler),
            cache_ttl_seconds=0,
        )


def mint_account(mint_auth=MINT, freeze_auth=FREEZE, supply="1000", decimals=6) -> dict:
    return {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {
            "parsed": {
                "type": "mint",
                "info": {
                    "mintAuthority": mint_auth,
                    "freezeAuthority": freeze_auth,
                    "supply": supply,
                    "decimals": decimals,
                    "isInitialized": True,
                },
            },
            "program": "spl-token",
        },
    }


def _clean(session) -> None:
    session.execute(delete(RiskScoreHistory))
    session.execute(delete(TokenRiskAssessment))
    session.execute(delete(WalletActivity))
    session.execute(delete(LiquidityAnalysisRecord))
    session.execute(delete(HolderAnalysis))
    session.execute(delete(TokenSecurityScan))
    session.execute(delete(Token2022ExtensionObservation))
    session.execute(delete(CreatorObservation))
    session.execute(delete(TokenAccountOwnerObservation))
    session.execute(delete(MetaplexCreatorObservation))
    session.execute(delete(MarketSnapshot))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(IntelligenceScan))
    session.execute(delete(RpcCooldownState))
    session.commit()


def test_mint_and_freeze_authority_detection() -> None:
    script = RpcScript()
    script.account = mint_account()

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(TOKEN_A, rpc)

    result = run(body())
    assert result.mint_authority_status == "VERIFIED_PRESENT"
    assert result.freeze_authority_status == "VERIFIED_PRESENT"
    assert result.mint_authority == MINT


def test_revoked_authorities() -> None:
    script = RpcScript()
    script.account = mint_account(mint_auth=None, freeze_auth=None)

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(TOKEN_A, rpc)

    result = run(body())
    assert result.mint_authority_status == "VERIFIED_REVOKED"
    assert result.freeze_authority_status == "VERIFIED_REVOKED"


def test_missing_blockchain_data() -> None:
    script = RpcScript()
    script.account = None

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(TOKEN_A, rpc)

    result = run(body())
    assert result.available is False
    assert result.mint_authority_status == "UNKNOWN"


def test_holder_concentration_excludes_lp_and_burn() -> None:
    script = RpcScript()
    script.largest = [
        {"address": PAIR, "uiAmount": 400, "amount": "400"},
        {"address": BURN, "uiAmount": 300, "amount": "300"},
        {"address": HOLDER, "uiAmount": 300, "amount": "300"},
    ]

    async def body():
        async with script.client() as rpc:
            return await analyze_holders(TOKEN_A, rpc, supply=1000, pair_address=PAIR)

    result = run(body())
    assert result.available is True
    assert result.top_holder_pct == pytest.approx(30.0)
    assert {item.classification for item in result.holders} >= {"liquidity_pool", "burn", "unknown_holder"}


def test_unknown_holders_without_supply() -> None:
    script = RpcScript()
    script.largest = [{"address": HOLDER, "uiAmount": 10}]

    async def body():
        async with script.client() as rpc:
            return await analyze_holders(TOKEN_A, rpc, supply=None)

    result = run(body())
    assert result.concentration_status == "UNKNOWN"
    assert result.top_holder_pct is None


def test_liquidity_lock_is_unknown_without_verification() -> None:
    token = Token(token_address=TOKEN_A, pair_address=PAIR, chain="solana")
    snap = MarketSnapshot(token_id=1, liquidity_usd=25000, recorded_at=utc_now())
    result = analyze_liquidity(token, [snap])
    assert result.lock_status == "UNKNOWN"
    assert "not independently verified" in result.lock_evidence.lower()


def test_trading_anomalies() -> None:
    now = datetime.now(timezone.utc)
    snaps = [
        MarketSnapshot(token_id=1, volume_24h=1000, liquidity_usd=20000, price_change_24h=2, recorded_at=now - timedelta(hours=3)),
        MarketSnapshot(token_id=1, volume_24h=1100, liquidity_usd=21000, price_change_24h=3, recorded_at=now - timedelta(hours=2)),
        MarketSnapshot(
            token_id=1,
            volume_24h=90000,
            liquidity_usd=5000,
            price_change_24h=90,
            buys_24h=50,
            sells_24h=50,
            recorded_at=now,
        ),
    ]
    result = analyze_trading(snaps)
    assert result.volume_spike is True
    assert result.liquidity_shock is True
    assert result.wash_trade_indicator is True


def test_wallet_activity_does_not_claim_identity() -> None:
    script = RpcScript()
    script.account = mint_account()

    async def body():
        async with script.client() as rpc:
            security = await analyze_token_security(TOKEN_A, rpc)
            return analyze_wallet_activity(security)

    result = run(body())
    assert result.deployer_status == "ESTIMATE"
    assert "not as verified human identity" in result.findings[0]


def test_insufficient_data_scoring() -> None:
    settings = get_settings()
    from app.services.holder_analysis import HolderAnalysisResult
    from app.services.liquidity_analysis import LiquidityAnalysisResult
    from app.services.token_security import TokenSecurityResult
    from app.services.trading_analysis import TradingAnalysisResult
    from app.services.wallet_analysis import WalletAnalysisResult

    scored = score_token_risk(
        settings,
        TokenSecurityResult(),
        HolderAnalysisResult(),
        LiquidityAnalysisResult(),
        TradingAnalysisResult(),
        WalletAnalysisResult(),
    )
    assert scored.status == INSUFFICIENT_DATA
    assert scored.risk_score is None
    assert scored.risk_band is None
    assert scored.observed_risk is None
    assert scored.completeness == 0
    assert scored.classification_reliable is False


def test_completeness_040_is_not_a_final_score() -> None:
    settings = get_settings()
    from app.services.holder_analysis import HolderAnalysisResult
    from app.services.liquidity_analysis import LiquidityAnalysisResult
    from app.services.token_security import TokenSecurityResult
    from app.services.trading_analysis import TradingAnalysisResult
    from app.services.wallet_analysis import WalletAnalysisResult

    scored = score_token_risk(
        settings,
        TokenSecurityResult(),
        HolderAnalysisResult(),
        LiquidityAnalysisResult(liquidity_usd=25000, lock_status="UNKNOWN", available=True),
        TradingAnalysisResult(available=True, risk_hint=70),
        WalletAnalysisResult(),
    )
    assert scored.completeness == pytest.approx(0.40)
    assert scored.status == INSUFFICIENT_DATA
    assert scored.risk_score is None
    assert scored.risk_band is None
    assert scored.observed_risk is not None
    assert scored.observed_risk_band is not None
    assert scored.classification_reliable is False
    assert set(scored.unknown_categories) == {
        "token_authorities",
        "holder_concentration",
        "developer_activity",
    }
    names = {item.name: item.status for item in scored.categories}
    assert names["token_authorities"] == "UNKNOWN"
    assert names["liquidity_risk"] == "ESTIMATE"
    assert names["trading_behavior"] == "ESTIMATE"


def test_low_confidence_is_provisional() -> None:
    settings = get_settings()
    from app.services.holder_analysis import HolderAnalysisResult
    from app.services.liquidity_analysis import LiquidityAnalysisResult
    from app.services.token_security import TokenSecurityResult
    from app.services.trading_analysis import TradingAnalysisResult
    from app.services.wallet_analysis import WalletAnalysisResult

    scored = score_token_risk(
        settings,
        TokenSecurityResult(
            available=True,
            mint_authority_status="VERIFIED_PRESENT",
            freeze_authority_status="VERIFIED_REVOKED",
            mint_authority=MINT,
        ),
        HolderAnalysisResult(),
        LiquidityAnalysisResult(liquidity_usd=2000, lock_status="UNKNOWN", available=True),
        TradingAnalysisResult(available=True, risk_hint=70),
        WalletAnalysisResult(),
    )
    assert scored.completeness >= 0.50
    assert scored.confidence < 0.50
    assert scored.status == LOW_CONFIDENCE
    assert scored.risk_score is None
    assert scored.risk_band is None
    assert scored.observed_risk is not None
    assert scored.classification_reliable is False
    assert "holder_concentration" in scored.unknown_categories
    assert "developer_activity" in scored.unknown_categories


def test_risk_score_with_authorities() -> None:
    settings = get_settings()
    from app.services.holder_analysis import HolderAnalysisResult
    from app.services.liquidity_analysis import LiquidityAnalysisResult
    from app.services.token_security import TokenSecurityResult
    from app.services.trading_analysis import TradingAnalysisResult
    from app.services.wallet_analysis import WalletAnalysisResult

    security = TokenSecurityResult(
        available=True,
        mint_authority_status="VERIFIED_PRESENT",
        freeze_authority_status="VERIFIED_PRESENT",
        mint_authority=MINT,
        freeze_authority=FREEZE,
    )
    holders = HolderAnalysisResult(available=True, concentration_status="suspicious", top_holder_pct=40)
    liquidity = LiquidityAnalysisResult(liquidity_usd=2000, lock_status="UNKNOWN", available=True)
    trading = TradingAnalysisResult(available=True, risk_hint=60)
    wallet = WalletAnalysisResult(available=True, deployer_status="ESTIMATE", risk_hint=55, findings=["estimate"])
    scored = score_token_risk(settings, security, holders, liquidity, trading, wallet)
    assert scored.status == "scored"
    assert scored.risk_score is not None
    assert scored.observed_risk == scored.risk_score
    assert scored.risk_score >= 60
    assert scored.completeness == pytest.approx(1.0)
    assert scored.confidence >= 0.50
    assert scored.classification_reliable is True
    assert scored.unknown_categories == []
    by_name = {item.name: item for item in scored.categories}
    assert by_name["token_authorities"].status == "VERIFIED"
    assert by_name["holder_concentration"].weight == pytest.approx(0.20)
    assert by_name["liquidity_risk"].reason


def test_legacy_scored_row_is_reclassified_by_current_thresholds() -> None:
    settings = get_settings()
    viewed = classify_measurements(
        settings,
        completeness=0.4,
        confidence=0.364,
        observed_risk=None,
        observed_risk_band=None,
        stored_risk_score=75,
        stored_risk_band="high",
    )
    assert viewed.status == INSUFFICIENT_DATA
    assert viewed.observed_risk == 75
    assert viewed.observed_risk_band == "high"
    assert viewed.risk_score is None
    assert viewed.risk_band is None
    assert viewed.classification_reliable is False


def test_configured_thresholds_are_respected() -> None:
    settings = get_settings().model_copy(
        update={"security_min_completeness": 0.90, "security_min_confidence": 0.90}
    )
    viewed = classify_measurements(
        settings,
        completeness=0.80,
        confidence=0.80,
        observed_risk=82,
        observed_risk_band="critical",
        stored_risk_score=82,
        stored_risk_band="critical",
    )
    assert viewed.status == INSUFFICIENT_DATA
    assert viewed.risk_score is None
    assert viewed.observed_risk == 82
    assert viewed.classification_reliable is False

    defaulted = classify_measurements(
        get_settings(),
        completeness=0.80,
        confidence=0.80,
        observed_risk=82,
        observed_risk_band="critical",
        stored_risk_score=82,
        stored_risk_band="critical",
    )
    assert defaulted.status == "scored"
    assert defaulted.risk_score == 82
    assert defaulted.classification_reliable is True


def test_rpc_rate_limit_then_success() -> None:
    script = RpcScript()
    script.account = mint_account(mint_auth=None, freeze_auth=None)
    script._rate_remaining = 1

    async def body():
        async with script.client() as rpc:
            return await analyze_token_security(TOKEN_A, rpc)

    result = run(body())
    assert result.mint_authority_status == "VERIFIED_REVOKED"


def test_rpc_failure_is_not_empty_success() -> None:
    script = RpcScript()
    script.account = httpx.Response(500, json={"error": "down"})

    async def body():
        async with script.client() as rpc:
            with pytest.raises(SolanaRpcError):
                await rpc.get_parsed_account(TOKEN_A)

    run(body())


def test_security_scan_persistence_and_history() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=TOKEN_A, name="Test", symbol="TST", chain="solana", pair_address=PAIR)
    session.add(token)
    session.commit()
    session.add(
        MarketSnapshot(
            token_id=token.id,
            liquidity_usd=25000,
            volume_24h=8000,
            buys_24h=20,
            sells_24h=10,
            recorded_at=utc_now(),
        )
    )
    session.commit()
    script = RpcScript()
    script.account = mint_account()
    script.largest = [{"address": HOLDER, "uiAmount": 200, "amount": "200"}]

    async def body():
        async with script.client() as rpc:
            first = await scan_tokens(session, rpc, token_address=TOKEN_A, force=True)
            second = await scan_tokens(session, rpc, token_address=TOKEN_A, force=True)
            return first, second

    first, second = run(body())
    assert first.tokens_scanned == 1
    assert second.tokens_scanned == 1
    history = list(session.scalars(select(RiskScoreHistory)).all())
    assert len(history) == 2
    security_rows = list(session.scalars(select(TokenSecurityScan)).all())
    assert len(security_rows) == 2
    session.close()
