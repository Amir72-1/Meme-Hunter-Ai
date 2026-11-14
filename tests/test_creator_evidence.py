"""Phase 9C.5 read-only unified creator evidence panel tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    CreatorObservation,
    IntelligenceScan,
    MetaplexCreatorObservation,
    Token,
    TokenAccountOwnerObservation,
)
from app.services.creator_evidence import MATCH_KIND, build_creator_evidence_panel
from app.services.opportunity_scoring import HIGH_OPPORTUNITY, classify_opportunity
from app.services.solana_rpc import SolanaRpcClient
from tests.test_security import FREEZE, MINT, _clean

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ATA = "Hold11111111111111111111111111111111111111"
OTHER = "Othr11111111111111111111111111111111111111"
FEE = "FeePay1111111111111111111111111111111111111"


def _token(session) -> Token:
    _clean(session)
    session.execute(delete(CreatorObservation))
    session.execute(delete(TokenAccountOwnerObservation))
    session.execute(delete(MetaplexCreatorObservation))
    session.commit()
    token = Token(token_address=MINT, symbol="D", name="Doge", chain="solana")
    session.add(token)
    session.commit()
    return token


def _mint_row(token_id: int, **kwargs) -> CreatorObservation:
    values = {
        "token_id": token_id,
        "mint_address": MINT,
        "inspection_status": "VERIFIED",
        "mint_init_status": "VERIFIED",
        "mint_init_signer": FREEZE,
        "fee_payer": FEE,
        "signer_addresses_json": f'["{FEE}","{FREEZE}"]',
        "create_program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "create_instruction": "initializeMint",
        "mint_init_signature": "Sig111",
        "mint_init_slot": 10,
        "slot": 10,
        "source": "solana_rpc",
        "observed_at": NOW,
        "recorded_at": NOW,
        "parser_notes": "init",
        "errors_json": "[]",
    }
    values.update(kwargs)
    return CreatorObservation(**values)


def _holder_row(token_id: int, **kwargs) -> TokenAccountOwnerObservation:
    values = {
        "token_id": token_id,
        "mint_address": MINT,
        "token_account_address": ATA,
        "token_account_owner": FREEZE,
        "token_account_amount": 12.5,
        "owner_classification": "WALLET",
        "classification_status": "VERIFIED",
        "classification_evidence": "system owner",
        "source": "solana_rpc",
        "observed_at": NOW,
        "recorded_at": NOW,
        "slot": 11,
        "parser_notes": "holder",
        "errors_json": "[]",
    }
    values.update(kwargs)
    return TokenAccountOwnerObservation(**values)


def _meta_row(token_id: int, **kwargs) -> MetaplexCreatorObservation:
    values = {
        "token_id": token_id,
        "mint_address": MINT,
        "metadata_account": "Meta11111111111111111111111111111111111111",
        "metadata_status": "AVAILABLE",
        "creator_vector_status": "VERIFIED",
        "creators_json": f'[{{"address":"{FREEZE}","verified":true,"share":100,"order":0}}]',
        "creator_count": 1,
        "verified_creator_count": 1,
        "source": "solana_rpc",
        "observed_at": NOW,
        "recorded_at": NOW,
        "slot": 12,
        "parser_notes": "meta",
        "errors_json": "[]",
    }
    values.update(kwargs)
    return MetaplexCreatorObservation(**values)


def test_all_three_sources_present() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all([_mint_row(token.id), _holder_row(token.id), _meta_row(token.id)])
        session.commit()
        panel = build_creator_evidence_panel(session, token)
        assert panel.mint_initialization is not None
        assert panel.mint_initialization.mint_init_signer == FREEZE
        assert panel.mint_initialization.fee_payer == FEE
        assert len(panel.token_accounts) == 1
        assert panel.token_accounts[0].token_account_owner == FREEZE
        assert panel.metaplex is not None
        assert panel.metaplex.creator_vector_status == "VERIFIED"
        assert panel.address_matches
        assert all(item.kind == MATCH_KIND for item in panel.address_matches)
    finally:
        session.close()


def test_each_source_independently_missing() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add(_mint_row(token.id))
        session.commit()
        panel = build_creator_evidence_panel(session, token)
        assert panel.mint_initialization is not None
        assert panel.token_accounts == []
        assert panel.metaplex is None
    finally:
        session.close()


def test_status_semantics_unknown_unavailable_verified_absent() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all(
            [
                _mint_row(token.id, inspection_status="UNKNOWN", mint_init_status="UNKNOWN", mint_init_signer=None),
                _holder_row(token.id, classification_status="UNAVAILABLE", owner_classification="UNKNOWN"),
                _meta_row(
                    token.id,
                    creator_vector_status="VERIFIED_ABSENT",
                    creators_json=None,
                    creator_count=0,
                    verified_creator_count=0,
                ),
            ]
        )
        session.commit()
        panel = build_creator_evidence_panel(session, token)
        assert panel.mint_initialization.inspection_status == "UNKNOWN"
        assert panel.token_accounts[0].classification_status == "UNAVAILABLE"
        assert panel.metaplex.creator_vector_status == "VERIFIED_ABSENT"
        assert panel.metaplex.creator_vector_status != "UNAVAILABLE"
    finally:
        session.close()


def test_multiple_creators_and_token_accounts() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all(
            [
                _mint_row(token.id),
                _holder_row(token.id),
                _holder_row(token.id, token_account_address=OTHER, token_account_owner=OTHER),
                _meta_row(
                    token.id,
                    creators_json=(
                        f'[{{"address":"{FREEZE}","verified":true,"share":60,"order":0}},'
                        f'{{"address":"{OTHER}","verified":false,"share":40,"order":1}}]'
                    ),
                    creator_count=2,
                    verified_creator_count=1,
                ),
            ]
        )
        session.commit()
        panel = build_creator_evidence_panel(session, token)
        assert len(panel.token_accounts) == 2
        assert panel.metaplex.creator_count == 2
    finally:
        session.close()


def test_exact_address_match_across_sources_is_not_identity() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all([_mint_row(token.id), _holder_row(token.id), _meta_row(token.id)])
        session.commit()
        panel = build_creator_evidence_panel(session, token)
        freeze_match = next(item for item in panel.address_matches if item.address == FREEZE)
        assert freeze_match.kind == "ADDRESS_MATCH_ACROSS_SOURCES"
        assert set(freeze_match.sources) == {
            "mint_initialization",
            "token_account_ownership",
            "metaplex_creators",
        }
        blob = " ".join(item.note.lower() for item in panel.address_matches)
        assert "same_person" not in blob
        assert "identity" in blob
        assert "not identity" in blob or "not identity" in panel.note.lower()
        assert all("SAME_PERSON" not in item.kind for item in panel.address_matches)
        assert "deployer identity" in freeze_match.note.lower() or "not" in freeze_match.note.lower()
    finally:
        session.close()


def test_strict_as_of_and_null_observed_at_and_no_future_leak() -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all(
            [
                _mint_row(token.id, inspection_status="UNKNOWN", observed_at=NOW),
                _mint_row(token.id, inspection_status="VERIFIED", observed_at=NOW + timedelta(hours=2)),
                _holder_row(token.id, classification_status="UNKNOWN", observed_at=NOW),
                _holder_row(
                    token.id,
                    classification_status="VERIFIED",
                    observed_at=NOW + timedelta(hours=2),
                ),
                _meta_row(token.id, creator_vector_status="VERIFIED_ABSENT", observed_at=NOW, creators_json=None),
                _meta_row(token.id, creator_vector_status="VERIFIED", observed_at=NOW + timedelta(hours=2)),
                _holder_row(
                    token.id,
                    token_account_address="Null11111111111111111111111111111111111111",
                    observed_at=None,
                    classification_status="VERIFIED",
                ),
            ]
        )
        session.commit()
        early = build_creator_evidence_panel(session, token, as_of=NOW + timedelta(minutes=30))
        assert early.mint_initialization.inspection_status == "UNKNOWN"
        assert early.token_accounts[0].classification_status == "UNKNOWN"
        assert early.metaplex.creator_vector_status == "VERIFIED_ABSENT"
        assert all(row.observed_at is not None for row in early.token_accounts)
        none = build_creator_evidence_panel(session, token, as_of=NOW - timedelta(minutes=1))
        assert none.mint_initialization is None
        assert none.token_accounts == []
        assert none.metaplex is None
        later = build_creator_evidence_panel(session, token, as_of=NOW + timedelta(hours=3))
        assert later.mint_initialization.inspection_status == "VERIFIED"
        assert later.metaplex.creator_vector_status == "VERIFIED"
    finally:
        session.close()


def test_api_is_read_only_and_makes_no_rpc_calls(client, monkeypatch) -> None:
    session = get_session_factory()()
    try:
        token = _token(session)
        session.add_all([_mint_row(token.id), _holder_row(token.id), _meta_row(token.id)])
        session.commit()

        def boom(*_args, **_kwargs):
            raise AssertionError("Solana RPC must not be used by the evidence panel")

        monkeypatch.setattr(SolanaRpcClient, "__init__", boom)
        before_init = session.scalar(select(func.count()).select_from(CreatorObservation))
        before_hold = session.scalar(select(func.count()).select_from(TokenAccountOwnerObservation))
        before_meta = session.scalar(select(func.count()).select_from(MetaplexCreatorObservation))
        before_scan = session.scalar(select(func.count()).select_from(IntelligenceScan))
        response = client.get(f"/api/v1/creator/evidence/{MINT}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["read_only"] is True
        assert payload["rpc_calls"] == 0
        assert payload["token_address"] == MINT
        assert payload["mint_initialization"]["available"] is True
        assert payload["token_account_ownership"]["observations"][0]["token_account_amount"] == 12.5
        assert payload["metaplex_creators"]["status"] == "VERIFIED"
        assert payload["address_matches"][0]["kind"] == MATCH_KIND
        assert "SAME_PERSON" not in str(payload)
        text = str(payload).lower()
        assert "not identity" in text or "not a person" in text or "not identified as people" in text
        missing = client.get("/api/v1/creator/evidence/UnknownMint1111111111111111111111111111111")
        assert missing.status_code == 404
        as_of = client.get(
            f"/api/v1/creator/evidence/{MINT}",
            params={"as_of": (NOW - timedelta(days=1)).isoformat()},
        )
        assert as_of.status_code == 200
        empty = as_of.json()
        assert empty["mint_initialization"]["available"] is False
        assert empty["mint_initialization"]["status"] == "UNKNOWN"
        assert empty["token_account_ownership"]["available"] is False
        assert empty["metaplex_creators"]["available"] is False
        session.expire_all()
        assert session.scalar(select(func.count()).select_from(CreatorObservation)) == before_init
        assert session.scalar(select(func.count()).select_from(TokenAccountOwnerObservation)) == before_hold
        assert session.scalar(select(func.count()).select_from(MetaplexCreatorObservation)) == before_meta
        assert session.scalar(select(func.count()).select_from(IntelligenceScan)) == before_scan
    finally:
        session.close()


def test_opportunity_floors_unchanged() -> None:
    settings = get_settings()
    assert settings.opportunity_min_completeness == 0.40
    assert settings.opportunity_min_confidence == 0.50
    assert settings.opportunity_high_threshold == 70
    status, label = classify_opportunity(
        settings,
        positive_opportunity_score=90,
        opportunity_score=90,
        data_completeness=0.39,
        confidence=0.90,
    )
    assert status != "SCORED"
    assert label != HIGH_OPPORTUNITY
