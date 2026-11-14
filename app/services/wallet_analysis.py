"""Deployer/activity notes from public mint fields only. No identity claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.token_security import TokenSecurityResult


@dataclass
class WalletAnalysisResult:
    deployer_address: str | None = None
    deployer_status: str = "UNKNOWN"
    provider: str = "solana_rpc"
    findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    available: bool = False
    risk_hint: float | None = None


def analyze_wallet_activity(security: TokenSecurityResult) -> WalletAnalysisResult:
    result = WalletAnalysisResult()
    if security.mint_authority_status == "VERIFIED_PRESENT" and security.mint_authority:
        result.available = True
        result.deployer_address = security.mint_authority
        result.deployer_status = "ESTIMATE"
        result.findings.append(
            "Mint authority is used as an ESTIMATE of the current privileged wallet, "
            "not as verified human identity or original deployer proof."
        )
        result.risk_hint = 55
        return result
    if security.mint_authority_status == "VERIFIED_REVOKED":
        result.available = True
        result.deployer_status = "VERIFIED_REVOKED"
        result.findings.append(
            "Mint authority is revoked. Original deployer is still UNKNOWN without transaction history."
        )
        result.risk_hint = 15
        return result
    result.deployer_status = "UNKNOWN"
    result.errors.append("Deployer/creator wallet could not be verified from public mint data.")
    result.findings.append("No transaction-history provider is configured; activity remains UNKNOWN.")
    return result
