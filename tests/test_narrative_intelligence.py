"""Narrative scoring, matching, and social adapter status tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import (
    IntelligenceScan,
    Narrative,
    NarrativeMention,
    NarrativeScoreHistory,
    NewsItem,
    SocialPost,
    OpportunityAssessment,
    Token,
    TokenNarrativeMatch,
)
from app.services.narrative_engine import detect_and_score_narratives, score_narrative, MentionRecord
from app.services.social_adapters import NOT_CONFIGURED, collect_social
from app.services.token_narrative_matcher import evaluate_match, match_tokens_to_narratives
from tests.dexscreener_fakes import run


def _clean(session) -> None:
    session.execute(delete(TokenNarrativeMatch))
    session.execute(delete(NarrativeScoreHistory))
    session.execute(delete(NarrativeMention))
    session.execute(delete(SocialPost))
    session.execute(delete(NewsItem))
    session.execute(delete(Narrative))
    session.execute(delete(OpportunityAssessment))
    session.execute(delete(Token))
    session.execute(delete(IntelligenceScan))
    session.commit()


def test_social_scan_without_credentials_is_not_configured() -> None:
    session = get_session_factory()()
    _clean(session)

    async def body():
        return await collect_social(session)

    result = run(body())
    statuses = {item.platform: item.status for item in result.adapters}
    assert statuses["reddit"] == NOT_CONFIGURED
    assert statuses["x"] == NOT_CONFIGURED
    assert statuses["telegram"] == NOT_CONFIGURED
    assert result.items_saved == 0
    session.close()


def test_missing_growth_is_not_invented() -> None:
    now = datetime.now(timezone.utc)
    mentions = [
        MentionRecord(
            source_type="news",
            source_id=1,
            source_name="CoinDesk",
            author_id=None,
            engagement=None,
            observed_at=now - timedelta(hours=1),
            keyword="openai",
            text="OpenAI news",
        )
    ]
    metrics = score_narrative(mentions, get_settings(), now)
    assert metrics["mention_growth"] is None
    assert metrics["engagement_growth"] is None
    assert metrics["mention_count"] == 1
    assert "omitted" in metrics["explanation"]


def test_narrative_detection_and_token_matching() -> None:
    session = get_session_factory()()
    _clean(session)
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            NewsItem(
                title="OpenAI announces ChatGPT upgrade",
                source="CoinDesk",
                url="https://example.com/n1",
                published_at=now - timedelta(hours=1),
                summary="Artificial intelligence labs ship a new model.",
                category="artificial_intelligence",
                normalized_title="openai announces chatgpt upgrade",
            ),
            NewsItem(
                title="Anthropic Claude update",
                source="The Verge AI",
                url="https://example.com/n2",
                published_at=now - timedelta(hours=2),
                summary="Another OpenAI competitor in artificial intelligence.",
                category="artificial_intelligence",
                normalized_title="anthropic claude update",
            ),
            Token(
                token_address="AiToken111111111111111111111111111111111111",
                name="Random Coin",
                symbol="OPENAI",
                chain="solana",
            ),
            Token(
                token_address="Weak22222222222222222222222222222222222222",
                name="AI Dog",
                symbol="AIDOGE",
                chain="solana",
            ),
        ]
    )
    session.commit()
    detected = detect_and_score_narratives(session)
    assert detected.narratives_upserted >= 1
    narrative = session.scalar(select(Narrative).where(Narrative.name == "AI breakthrough"))
    assert narrative is not None
    assert narrative.mention_count == 2
    assert narrative.independent_sources == 2
    matched = match_tokens_to_narratives(session)
    assert matched.matches_saved >= 1
    rows = list(session.scalars(select(TokenNarrativeMatch)).all())
    methods = {row.method for row in rows}
    assert methods & {"symbol_overlap", "symbol_and_source"}
    session.close()


def test_name_only_generic_match_is_rejected() -> None:
    token = Token(token_address="x", name="AI Dog", symbol="DOG", chain="solana")
    narrative = Narrative(
        name="AI breakthrough",
        category="artificial_intelligence",
        keywords='["openai", "ai"]',
        first_detected_at=datetime.now(timezone.utc),
        last_detected_at=datetime.now(timezone.utc),
    )
    assert evaluate_match(token, narrative, []) is None
