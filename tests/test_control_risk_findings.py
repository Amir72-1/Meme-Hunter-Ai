"""Phase 9C.2 explainable sellability and control-risk findings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    HistoricalEvaluation,
    MarketSnapshot,
    PointInTimeRun,
    Token,
    Token2022ExtensionObservation,
    TokenSecurityScan,
)
from app.services.control_risk_findings import (
    CONFIDENTIAL_TRANSFER_ENABLED,
    DEFAULT_ACCOUNT_FROZEN,
    FREEZE_AUTHORITY_ACTIVE,
    METADATA_POINTER_PRESENT,
    MINT_AUTHORITY_ACTIVE,
    MINT_CLOSE_AUTHORITY_PRESENT,
    NON_TRANSFERABLE,
    PAUSABLE_TOKEN,
    PERMANENT_DELEGATE_PRESENT,
    SCALED_UI_AMOUNT_CONFIGURED,
    TOKEN_METADATA_PRESENT,
    TRANSFER_FEE_AUTHORITY_PRESENT,
    TRANSFER_FEE_CONFIGURED,
    TRANSFER_HOOK_CONFIGURED,
    UNAVAILABLE,
    UNKNOWN,
    UNKNOWN_EXTENSION_PRESENT,
    VERIFIED_ABSENT,
    VERIFIED_PRESENT,
    evaluate_control_findings,
    finding_by_code,
    findings_for_token,
)
from app.services.opportunity_scoring import (
    HIGH_OPPORTUNITY,
    classify_opportunity,
    positive_weights,
)
from app.services.solana_pda import b58encode
from app.services.solana_rpc import RpcAccountResult
from app.services.token_2022_extensions import inspect_token_2022_mint
from app.services.token_security import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from tests.test_security import _clean
from tests.test_token_2022_extensions import _t22_account

MINT = b58encode(bytes(range(32)))
OTHER_MINT = b58encode(bytes(range(1, 33)))
HOOK = b58encode(bytes([2] * 32))
DELEGATE = b58encode(bytes([3] * 32))
CLOSE = b58encode(bytes([4] * 32))
META_PTR = b58encode(bytes([5] * 32))
AUTH = b58encode(bytes([6] * 32))
T0 = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def _obs(**kwargs) -> Token2022ExtensionObservation:
    values = dict(
        token_id=1,
        mint_address=MINT,
        token_program=TOKEN_2022_PROGRAM,
        inspection_status="AVAILABLE",
        extension_types_json="[]",
        parsed_fields_json="{}",
        parser_complete=1,
        source="solana_rpc",
        observed_at=T0,
        recorded_at=T0 + timedelta(seconds=1),
        slot=50,
    )
    values.update(kwargs)
    return Token2022ExtensionObservation(**values)


def _scan(**kwargs) -> TokenSecurityScan:
    values = dict(
        token_id=1,
        scanned_at=T0,
        source="solana_rpc",
        observed_at=T0,
        recorded_at=T0 + timedelta(seconds=1),
        slot=50,
        mint_authority_status="UNKNOWN",
        freeze_authority_status="UNKNOWN",
        token_program=TOKEN_2022_PROGRAM,
    )
    values.update(kwargs)
    return TokenSecurityScan(**values)


def _eval(obs=None, scan=None, mint: str = MINT, when=T0):
    return evaluate_control_findings(mint, t22_observation=obs, security_scan=scan, signal_time=when)


def test_transfer_fee_configured() -> None:
    obs = _obs(
        extension_types_json='["transferFeeConfig"]',
        transfer_fee_bps=150,
        transfer_fee_max="1000000",
    )
    item = finding_by_code(_eval(obs), TRANSFER_FEE_CONFIGURED)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert item.verified is True
    assert item.evidence["transfer_fee_bps"] == 150
    assert "malicious" in item.explanation.lower() or "not automatically malicious" in item.explanation.lower()


def test_transfer_fee_authority_present() -> None:
    obs = _obs(
        extension_types_json='["transferFeeConfig"]',
        transfer_fee_bps=10,
        transfer_fee_config_authority=AUTH,
        transfer_fee_withdraw_authority=CLOSE,
    )
    item = finding_by_code(_eval(obs), TRANSFER_FEE_AUTHORITY_PRESENT)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert item.evidence["transfer_fee_config_authority"] == AUTH


def test_transfer_hook_configured() -> None:
    obs = _obs(extension_types_json='["transferHook"]', transfer_hook_program_id=HOOK, transfer_hook_authority=AUTH)
    item = finding_by_code(_eval(obs), TRANSFER_HOOK_CONFIGURED)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert "malicious" in item.explanation.lower()
    assert item.severity == "SELLABILITY_CONCERN"


def test_permanent_delegate_present() -> None:
    obs = _obs(extension_types_json='["permanentDelegate"]', permanent_delegate=DELEGATE)
    item = finding_by_code(_eval(obs), PERMANENT_DELEGATE_PRESENT)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert "theft" in item.explanation.lower()


def test_default_frozen_state() -> None:
    obs = _obs(extension_types_json='["defaultAccountState"]', default_account_state="frozen")
    item = finding_by_code(_eval(obs), DEFAULT_ACCOUNT_FROZEN)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert "thaw" in item.limitations.lower()


def test_non_transferable_extension() -> None:
    obs = _obs(extension_types_json='["nonTransferable"]')
    item = finding_by_code(_eval(obs), NON_TRANSFERABLE)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert item.severity == "SELLABILITY_CONCERN"


def test_pausable_extension() -> None:
    obs = _obs(extension_types_json='["pausable"]', pausable_authority=AUTH, pausable_paused=0)
    item = finding_by_code(_eval(obs), PAUSABLE_TOKEN)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert "not claimed to be paused" in item.explanation.lower() or "paused flag is false" in item.explanation.lower()


def test_mint_close_authority() -> None:
    obs = _obs(extension_types_json='["mintCloseAuthority"]', mint_close_authority=CLOSE)
    item = finding_by_code(_eval(obs), MINT_CLOSE_AUTHORITY_PRESENT)
    assert item is not None
    assert item.status == VERIFIED_PRESENT


def test_confidential_transfer() -> None:
    obs = _obs(
        extension_types_json='["confidentialTransferMint"]',
        confidential_transfer_present=1,
        confidential_auto_approve=0,
    )
    item = finding_by_code(_eval(obs), CONFIDENTIAL_TRANSFER_ENABLED)
    assert item is not None
    assert item.status == VERIFIED_PRESENT
    assert "hidden" in item.explanation.lower() or "hidden" in item.limitations.lower()


def test_metadata_pointer_and_token_metadata() -> None:
    obs = _obs(
        extension_types_json='["metadataPointer", "tokenMetadata"]',
        metadata_pointer_address=META_PTR,
        token_metadata_present=1,
        parsed_fields_json='{"tokenMetadata": {"mint": "%s", "name": "Demo"}}' % MINT,
    )
    findings = _eval(obs)
    pointer = finding_by_code(findings, METADATA_POINTER_PRESENT)
    meta = finding_by_code(findings, TOKEN_METADATA_PRESENT)
    assert pointer is not None and pointer.status == VERIFIED_PRESENT
    assert meta is not None and meta.status == VERIFIED_PRESENT
    assert "immutable" in pointer.explanation.lower()


def test_scaled_ui_amount() -> None:
    obs = _obs(extension_types_json='["scaledUiAmount"]', scaled_ui_amount_multiplier="1.5")
    item = finding_by_code(_eval(obs), SCALED_UI_AMOUNT_CONFIGURED)
    assert item is not None
    assert item.status == VERIFIED_PRESENT


def test_active_mint_and_freeze_authority() -> None:
    scan = _scan(
        mint_authority=AUTH,
        mint_authority_status="VERIFIED_PRESENT",
        freeze_authority=CLOSE,
        freeze_authority_status="VERIFIED_PRESENT",
    )
    findings = _eval(scan=scan)
    mint = finding_by_code(findings, MINT_AUTHORITY_ACTIVE)
    freeze = finding_by_code(findings, FREEZE_AUTHORITY_ACTIVE)
    assert mint is not None and mint.status == VERIFIED_PRESENT
    assert freeze is not None and freeze.status == VERIFIED_PRESENT
    assert "privilege" in mint.explanation.lower()


def test_verified_absent_extension() -> None:
    obs = _obs(inspection_status="NO_EXTENSIONS", extension_types_json="[]", parser_complete=1)
    item = finding_by_code(_eval(obs), TRANSFER_HOOK_CONFIGURED)
    assert item is not None
    assert item.status == VERIFIED_ABSENT
    assert item.verified is True
    assert "not proof" in item.limitations.lower() or "not a safety" in item.limitations.lower()


def test_missing_observation_returns_unknown() -> None:
    findings = _eval()
    for code in (TRANSFER_HOOK_CONFIGURED, MINT_AUTHORITY_ACTIVE):
        item = finding_by_code(findings, code)
        assert item is not None
        assert item.status == UNKNOWN
        assert item.source_observation_id is None
        assert item.verified is False


def test_incomplete_parser_output_is_unknown_or_unavailable() -> None:
    obs = _obs(
        inspection_status="UNAVAILABLE",
        parser_complete=0,
        extension_types_json='["transferHook", "unparseableExtension"]',
        transfer_hook_program_id=HOOK,
    )
    hook = finding_by_code(_eval(obs), TRANSFER_HOOK_CONFIGURED)
    fee = finding_by_code(_eval(obs), TRANSFER_FEE_CONFIGURED)
    assert hook is not None and hook.status == UNKNOWN
    assert fee is not None and fee.status in {UNKNOWN, UNAVAILABLE}
    assert hook.verified is False


def test_invalid_field_types() -> None:
    obs = _obs(extension_types_json='["defaultAccountState"]', default_account_state="not-a-state")
    item = finding_by_code(_eval(obs), DEFAULT_ACCOUNT_FROZEN)
    assert item is not None
    assert item.status == UNKNOWN


def test_invalid_transfer_fee_values() -> None:
    high = finding_by_code(
        _eval(_obs(extension_types_json='["transferFeeConfig"]', transfer_fee_bps=10001)),
        TRANSFER_FEE_CONFIGURED,
    )
    missing = finding_by_code(
        _eval(_obs(extension_types_json='["transferFeeConfig"]', transfer_fee_bps=None)),
        TRANSFER_FEE_CONFIGURED,
    )
    assert high is not None and high.status == UNKNOWN
    assert missing is not None and missing.status == UNKNOWN


def test_invalid_public_keys() -> None:
    obs = _obs(extension_types_json='["permanentDelegate"]', permanent_delegate="not-a-pubkey")
    item = finding_by_code(_eval(obs), PERMANENT_DELEGATE_PRESENT)
    assert item is not None
    assert item.status == UNKNOWN
    scan = _scan(mint_authority="zzz", mint_authority_status="VERIFIED_PRESENT")
    mint = finding_by_code(_eval(scan=scan), MINT_AUTHORITY_ACTIVE)
    assert mint is not None and mint.status == UNKNOWN


def test_requested_mint_mismatch() -> None:
    obs = _obs(mint_address=OTHER_MINT, extension_types_json='["transferHook"]', transfer_hook_program_id=HOOK)
    item = finding_by_code(_eval(obs), TRANSFER_HOOK_CONFIGURED)
    assert item is not None
    assert item.status == UNKNOWN
    assert "different mint" in item.limitations.lower() or "does not match" in item.explanation.lower()


def test_inspect_rejects_token_metadata_for_other_mint() -> None:
    account = _t22_account(
        [
            {
                "extension": "tokenMetadata",
                "state": {"mint": OTHER_MINT, "name": "Nope", "symbol": "NO", "uri": "https://example.com"},
            }
        ]
    )
    result = inspect_token_2022_mint(MINT, RpcAccountResult(value=account, slot=1, observed_at=T0))
    assert result.inspection_status == "UNAVAILABLE"
    assert result.parser_complete is False


def test_wrong_account_owner() -> None:
    obs = _obs(token_program="11111111111111111111111111111111", extension_types_json='["transferHook"]')
    item = finding_by_code(_eval(obs), TRANSFER_HOOK_CONFIGURED)
    assert item is not None
    assert item.status == UNAVAILABLE


def test_pit_excludes_later_observations() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=MINT, name="PIT", symbol="PIT", chain="solana")
    session.add(token)
    session.commit()
    early = _obs(
        token_id=token.id,
        inspection_status="NO_EXTENSIONS",
        observed_at=T0,
        recorded_at=T0 + timedelta(seconds=1),
        slot=10,
    )
    later = _obs(
        token_id=token.id,
        extension_types_json='["transferHook"]',
        transfer_hook_program_id=HOOK,
        observed_at=T0 + timedelta(hours=3),
        recorded_at=T0 + timedelta(hours=3, seconds=1),
        slot=99,
    )
    session.add_all([early, later])
    session.commit()
    report = findings_for_token(session, token, T0 + timedelta(minutes=5))
    hook = finding_by_code(report.findings, TRANSFER_HOOK_CONFIGURED)
    assert hook is not None
    assert hook.status == VERIFIED_ABSENT
    assert hook.source_observation_id == early.id
    assert hook.slot == 10
    session.close()


def test_legacy_security_records_remain_unchanged() -> None:
    session = get_session_factory()()
    _clean(session)
    token = Token(token_address=MINT, name="Legacy", symbol="LEG", chain="solana")
    session.add(token)
    session.commit()
    scanned = datetime(2026, 1, 2, tzinfo=timezone.utc)
    legacy = TokenSecurityScan(
        token_id=token.id,
        scanned_at=scanned,
        source="solana_rpc",
        observed_at=scanned,
        recorded_at=scanned,
        slot=7,
        mint_authority=AUTH,
        mint_authority_status="VERIFIED_PRESENT",
        freeze_authority_status="UNKNOWN",
        token_standard="spl-token",
        findings_json="[]",
        errors_json="[]",
    )
    session.add(legacy)
    session.commit()
    legacy_id, legacy_slot, legacy_std = legacy.id, legacy.slot, legacy.token_standard
    findings_for_token(session, token, scanned + timedelta(minutes=1))
    kept = session.get(TokenSecurityScan, legacy_id)
    assert kept is not None
    assert kept.slot == legacy_slot
    assert kept.token_standard == legacy_std
    assert kept.mint_authority == AUTH
    session.close()


def test_opportunity_scoring_and_thresholds_unchanged() -> None:
    settings = get_settings()
    assert settings.opportunity_min_completeness == 0.40
    assert settings.opportunity_min_confidence == 0.50
    assert settings.opportunity_high_threshold == 70.0
    assert settings.security_min_completeness == 0.50
    assert settings.security_min_confidence == 0.50
    assert settings.security_scan_limit == 25
    weights = positive_weights(settings)
    assert weights["market_momentum"] == settings.opportunity_weight_momentum
    assert weights["narrative_momentum"] == settings.opportunity_weight_narrative
    assert weights["early_discovery"] == settings.opportunity_weight_discovery
    assert weights["liquidity_quality"] == settings.opportunity_weight_liquidity
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=80,
        opportunity_score=80,
        data_completeness=0.80,
        confidence=0.70,
    )
    assert label == HIGH_OPPORTUNITY
    assert status == "SCORED"


def test_classic_spl_verified_absent_and_snapshots_untouched() -> None:
    session = get_session_factory()()
    _clean(session)
    eval_row = HistoricalEvaluation(horizon="next_observation", explanation="keep-9c2")
    pit_row = PointInTimeRun(status="completed", candidates_count=1, summary_json='{"keep":true}')
    token = Token(token_address=MINT, name="SPL", symbol="SPL", chain="solana")
    session.add_all([eval_row, pit_row, token])
    session.commit()
    snap = MarketSnapshot(token_id=token.id, recorded_at=T0, price_usd=1.0, liquidity_usd=10.0)
    obs = _obs(
        token_id=token.id,
        token_program=TOKEN_PROGRAM,
        inspection_status="UNSUPPORTED",
        parser_complete=1,
        parser_notes="Classic SPL Token mint is outside Token-2022 extension inspection.",
    )
    session.add_all([snap, obs])
    session.commit()
    snap_id, liq = snap.id, snap.liquidity_usd
    eval_id, text = eval_row.id, eval_row.explanation
    report = findings_for_token(session, token, T0 + timedelta(minutes=1))
    hook = finding_by_code(report.findings, TRANSFER_HOOK_CONFIGURED)
    assert hook is not None and hook.status == VERIFIED_ABSENT
    kept_snap = session.get(MarketSnapshot, snap_id)
    assert kept_snap is not None and kept_snap.liquidity_usd == liq
    kept_eval = session.get(HistoricalEvaluation, eval_id)
    assert kept_eval is not None and kept_eval.explanation == text
    session.close()


def test_unknown_extension_is_preserved_without_clean_class() -> None:
    obs = _obs(extension_types_json='["futureControlWidget"]')
    findings = _eval(obs)
    extra = finding_by_code(findings, UNKNOWN_EXTENSION_PRESENT)
    assert extra is not None
    assert extra.status == VERIFIED_PRESENT
    assert extra.evidence["extension_types"] == ["futureControlWidget"]
    assert "safe" in extra.limitations.lower() or "classification" in extra.explanation.lower()
    hook = finding_by_code(findings, TRANSFER_HOOK_CONFIGURED)
    assert hook is not None and hook.status == VERIFIED_ABSENT
