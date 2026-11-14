"""Link narratives to discovered tokens with explicit evidence, not name-only guesses."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Narrative, NewsItem, Token, TokenNarrativeMatch, utc_now

logger = logging.getLogger("meme_hunter.matcher")

GENERIC_TERMS = {
    "coin",
    "token",
    "sol",
    "solana",
    "meme",
    "inu",
    "dog",
    "cat",
    "ai",
    "the",
    "and",
    "for",
    "dao",
}

_WORD = re.compile(r"[a-z0-9]{3,}")


@dataclass
class MatchScanResult:
    status: str
    matches_saved: int = 0
    weak_skipped: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def match_tokens_to_narratives(
    session: Session,
    settings: Settings | None = None,
) -> MatchScanResult:
    settings = settings or get_settings()
    result = MatchScanResult(status="running", started_at=datetime.now(timezone.utc))
    tokens = list(session.scalars(select(Token)).all())
    narratives = list(session.scalars(select(Narrative)).all())
    news_by_narrative: dict[int, list[NewsItem]] = {}
    for item in session.scalars(select(NewsItem).where(NewsItem.narrative_id.is_not(None))).all():
        news_by_narrative.setdefault(item.narrative_id, []).append(item)  # type: ignore[arg-type]

    for token in tokens:
        for narrative in narratives:
            decision = evaluate_match(token, narrative, news_by_narrative.get(narrative.id, []))
            if decision is None:
                continue
            if decision["confidence"] < settings.narrative_match_min_confidence:
                result.weak_skipped += 1
                continue
            existing = session.scalar(
                select(TokenNarrativeMatch).where(
                    TokenNarrativeMatch.token_id == token.id,
                    TokenNarrativeMatch.narrative_id == narrative.id,
                )
            )
            if existing is None:
                existing = TokenNarrativeMatch(
                    token_id=token.id,
                    narrative_id=narrative.id,
                    explanation=decision["explanation"],
                    confidence=decision["confidence"],
                    method=decision["method"],
                    matching_keywords=json.dumps(decision["keywords"]),
                    source_refs=json.dumps(decision["source_refs"]),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(existing)
            else:
                existing.explanation = decision["explanation"]
                existing.confidence = decision["confidence"]
                existing.method = decision["method"]
                existing.matching_keywords = json.dumps(decision["keywords"])
                existing.source_refs = json.dumps(decision["source_refs"])
                existing.updated_at = utc_now()
            result.matches_saved += 1

    session.commit()
    result.status = "completed"
    result.finished_at = datetime.now(timezone.utc)
    return result


def evaluate_match(token: Token, narrative: Narrative, news_items: list[NewsItem]) -> dict | None:
    symbol = (token.symbol or "").strip()
    name = (token.name or "").strip()
    if not symbol and not name:
        return None

    keywords = []
    if narrative.keywords:
        try:
            loaded = json.loads(narrative.keywords)
            if isinstance(loaded, list):
                keywords = [str(item).lower() for item in loaded]
        except json.JSONDecodeError:
            keywords = []
    keywords.extend(_WORD.findall((narrative.name or "").lower()))
    keywords = [kw for kw in dict.fromkeys(keywords) if kw not in GENERIC_TERMS and len(kw) >= 3]

    symbol_l = symbol.lower()
    name_l = name.lower()
    name_hits = [kw for kw in keywords if kw in name_l]
    symbol_hit = bool(symbol_l and len(symbol_l) >= 3 and (symbol_l in keywords or any(kw == symbol_l for kw in keywords)))

    source_refs: list[str] = []
    news_mentions_token = False
    for item in news_items:
        blob = f"{item.title} {item.summary or ''}".lower()
        if symbol_l and len(symbol_l) >= 3 and re.search(rf"\b{re.escape(symbol_l)}\b", blob):
            news_mentions_token = True
            source_refs.append(item.url)
        elif name_l and len(name_l) >= 4 and name_l in blob:
            news_mentions_token = True
            source_refs.append(item.url)

    if symbol_hit and news_mentions_token:
        return {
            "method": "symbol_and_source",
            "confidence": 0.86,
            "keywords": [symbol] + name_hits,
            "source_refs": source_refs[:5],
            "explanation": (
                f"Token symbol '{symbol}' matches narrative '{narrative.name}' and appears in "
                f"{len(source_refs)} stored source(s)."
            ),
        }
    if symbol_hit and not name_hits:
        return {
            "method": "symbol_overlap",
            "confidence": 0.55,
            "keywords": [symbol],
            "source_refs": source_refs[:5],
            "explanation": (
                f"Token symbol '{symbol}' overlaps narrative keywords for '{narrative.name}'. "
                "No supporting news/social source cited the token. This is not proof of a real connection."
            ),
        }
    if name_hits and news_mentions_token:
        return {
            "method": "name_and_source",
            "confidence": 0.72,
            "keywords": name_hits,
            "source_refs": source_refs[:5],
            "explanation": (
                f"Token name '{name}' contains {name_hits} and stored sources also mention the token."
            ),
        }
    if name_hits:
        # Name-only hits are hypotheses, stored only if distinctive and long.
        distinctive = [hit for hit in name_hits if len(hit) >= 5]
        if not distinctive:
            return None
        return {
            "method": "name_keyword_hypothesis",
            "confidence": 0.28,
            "keywords": distinctive,
            "source_refs": [],
            "explanation": (
                f"Token name '{name}' contains narrative keyword(s) {distinctive} for "
                f"'{narrative.name}'. Name overlap alone is not treated as a confirmed link."
            ),
        }
    return None
