"""Detect and score narratives from stored news and social posts."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    IntelligenceScan,
    Narrative,
    NarrativeMention,
    NarrativeScoreHistory,
    NewsItem,
    SocialPost,
    utc_now,
)

logger = logging.getLogger("meme_hunter.narratives")

TOPIC_CATALOG: tuple[dict[str, object], ...] = (
    {
        "name": "AI breakthrough",
        "category": "artificial_intelligence",
        "keywords": ("openai", "chatgpt", "anthropic", "claude", "gemini", "llm", "gpt-4", "gpt-5", "artificial intelligence"),
    },
    {
        "name": "Viral meme",
        "category": "internet_culture",
        "keywords": ("viral meme", "goes viral", "meme coin", "dogwifhat", "pepe"),
    },
    {
        "name": "Celebrity controversy",
        "category": "celebrity",
        "keywords": ("celebrity", "hollywood", "elon musk", "taylor swift", "kanye"),
    },
    {
        "name": "Major sporting event",
        "category": "sports",
        "keywords": ("world cup", "super bowl", "nba finals", "premier league", "olympics", "ufc"),
    },
    {
        "name": "Political announcement",
        "category": "politics",
        "keywords": ("election", "president", "white house", "sanctions", "congress", "prime minister"),
    },
    {
        "name": "New technology trend",
        "category": "technology",
        "keywords": ("apple", "google", "microsoft", "chip", "semiconductor", "robotics"),
    },
    {
        "name": "Gaming announcement",
        "category": "gaming",
        "keywords": ("playstation", "xbox", "nintendo", "steam", "game release", "esports"),
    },
    {
        "name": "Crypto ecosystem development",
        "category": "cryptocurrency",
        "keywords": ("solana", "bitcoin", "ethereum", "sec", "etf", "binance", "coinbase", "airdrop"),
    },
)

_WORD = re.compile(r"[a-z0-9][a-z0-9+\-]{2,}")


@dataclass
class MentionRecord:
    source_type: str
    source_id: int
    source_name: str
    author_id: str | None
    engagement: int | None
    observed_at: datetime | None
    keyword: str | None
    text: str


@dataclass
class NarrativeScanResult:
    status: str
    narratives_upserted: int = 0
    mentions_saved: int = 0
    scores_saved: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def detect_and_score_narratives(
    session: Session,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> NarrativeScanResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    result = NarrativeScanResult(status="running", started_at=now)
    scan_row = IntelligenceScan(scan_type="narratives", started_at=now, status="running")
    session.add(scan_row)
    session.commit()

    mentions_by_name: dict[str, list[MentionRecord]] = defaultdict(list)
    categories: dict[str, str] = {}
    keywords_by_name: dict[str, set[str]] = defaultdict(set)

    for item in session.scalars(select(NewsItem)).all():
        text = f"{item.title} {item.summary or ''}"
        matches = _match_topics(text)
        for name, category, keyword in matches:
            categories[name] = category
            keywords_by_name[name].add(keyword)
            mentions_by_name[name].append(
                MentionRecord(
                    source_type="news",
                    source_id=item.id,
                    source_name=item.source,
                    author_id=None,
                    engagement=None,
                    observed_at=item.published_at or item.created_at,
                    keyword=keyword,
                    text=text,
                )
            )
            item.narrative_id = None  # set after upsert

    for post in session.scalars(select(SocialPost)).all():
        matches = _match_topics(post.text)
        for name, category, keyword in matches:
            categories[name] = category
            keywords_by_name[name].add(keyword)
            mentions_by_name[name].append(
                MentionRecord(
                    source_type="social",
                    source_id=post.id,
                    source_name=post.source,
                    author_id=post.author_id,
                    engagement=post.engagement,
                    observed_at=post.published_at or post.created_at,
                    keyword=keyword,
                    text=post.text,
                )
            )

    news_by_id = {item.id: item for item in session.scalars(select(NewsItem)).all()}
    social_by_id = {item.id: item for item in session.scalars(select(SocialPost)).all()}

    for name, mentions in mentions_by_name.items():
        narrative = _upsert_narrative(
            session,
            name=name,
            category=categories.get(name),
            keywords=sorted(keywords_by_name[name]),
            mentions=mentions,
            now=now,
        )
        session.execute(delete(NarrativeMention).where(NarrativeMention.narrative_id == narrative.id))
        for mention in mentions:
            session.add(
                NarrativeMention(
                    narrative_id=narrative.id,
                    source_type=mention.source_type,
                    source_id=mention.source_id,
                    keyword=mention.keyword,
                    source_name=mention.source_name,
                    author_id=mention.author_id,
                    engagement=mention.engagement,
                    observed_at=mention.observed_at,
                    created_at=utc_now(),
                )
            )
            if mention.source_type == "news" and mention.source_id in news_by_id:
                news_by_id[mention.source_id].narrative_id = narrative.id
            if mention.source_type == "social" and mention.source_id in social_by_id:
                social_by_id[mention.source_id].narrative_id = narrative.id
        result.mentions_saved += len(mentions)

        metrics = score_narrative(mentions, settings, now, first_seen=narrative.first_detected_at)
        narrative.mention_count = metrics["mention_count"]
        narrative.mention_growth = metrics["mention_growth"]
        narrative.engagement_growth = metrics["engagement_growth"]
        narrative.independent_sources = metrics["independent_sources"]
        narrative.independent_authors = metrics["independent_authors"]
        narrative.recency_score = metrics["recency"]
        narrative.acceleration = metrics["acceleration"]
        narrative.persistence = metrics["persistence"]
        narrative.spam_score = metrics["spam_score"]
        narrative.confidence = metrics["confidence"]
        narrative.data_completeness = metrics["data_completeness"]
        narrative.trend_score = metrics["score"]
        narrative.explanation = metrics["explanation"]
        narrative.last_detected_at = now
        session.add(
            NarrativeScoreHistory(
                narrative_id=narrative.id,
                score=metrics["score"],
                mention_count=metrics["mention_count"],
                mention_growth=metrics["mention_growth"],
                engagement_growth=metrics["engagement_growth"],
                source_diversity=metrics["source_diversity"],
                recency_score=metrics["recency"],
                acceleration=metrics["acceleration"],
                persistence=metrics["persistence"],
                spam_score=metrics["spam_score"],
                confidence=metrics["confidence"],
                data_completeness=metrics["data_completeness"],
                components_json=json.dumps(metrics["components"]),
                recorded_at=now,
            )
        )
        result.scores_saved += 1
        result.narratives_upserted += 1

    session.commit()
    result.status = "completed"
    result.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = result.finished_at
    scan_row.status = result.status
    scan_row.items_fetched = result.narratives_upserted
    scan_row.items_saved = result.mentions_saved
    scan_row.details_json = json.dumps({"scores_saved": result.scores_saved})
    scan_row.errors_json = json.dumps(result.errors)
    session.commit()
    return result


def score_narrative(
    mentions: list[MentionRecord],
    settings: Settings,
    now: datetime,
    first_seen: datetime | None = None,
) -> dict[str, object]:
    recent_hours = settings.narrative_recent_hours
    recent_cut = now - timedelta(hours=recent_hours)
    prev_cut = now - timedelta(hours=recent_hours * 2)
    accel_cut = now - timedelta(hours=recent_hours / 2)
    older_cut = now - timedelta(hours=recent_hours * 1.5)

    recent = [m for m in mentions if _aware(m.observed_at) and _aware(m.observed_at) >= recent_cut]
    previous = [
        m
        for m in mentions
        if _aware(m.observed_at) and prev_cut <= _aware(m.observed_at) < recent_cut
    ]
    accel_recent = [
        m for m in mentions if _aware(m.observed_at) and _aware(m.observed_at) >= accel_cut
    ]
    accel_prev = [
        m
        for m in mentions
        if _aware(m.observed_at) and older_cut <= _aware(m.observed_at) < accel_cut
    ]

    mention_count = len(mentions)
    mention_growth = _growth(len(recent), len(previous), previous_empty=len(previous) == 0)
    recent_eng = _engagement_total(recent)
    prev_eng = _engagement_total(previous)
    engagement_growth = None
    if recent_eng is None and prev_eng is None:
        engagement_growth = None
    else:
        engagement_growth = _growth(recent_eng or 0, prev_eng or 0, previous_empty=(prev_eng is None))

    sources = {m.source_name for m in mentions if m.source_name}
    authors = {m.author_id for m in mentions if m.author_id}
    source_diversity = (len(sources) / mention_count) if mention_count else None
    newest = max((_aware(m.observed_at) for m in mentions if m.observed_at), default=None)
    recency = None
    if newest is not None:
        hours = max((now - newest).total_seconds() / 3600, 0)
        recency = math.exp(-hours / max(recent_hours, 1))
    acceleration = _growth(len(accel_recent), len(accel_prev), previous_empty=len(accel_prev) == 0)
    persistence = None
    origin = _aware(first_seen) or min(
        (_aware(m.observed_at) for m in mentions if m.observed_at),
        default=None,
    )
    if origin is not None:
        age_hours = max((now - origin).total_seconds() / 3600, 0)
        still_active = 1.0 if recent else 0.0
        persistence = min(age_hours / 48, 1.0) * still_active

    spam_score = 0.0
    if mention_count:
        top_share = max(
            (sum(1 for m in mentions if m.source_name == source) / mention_count for source in sources),
            default=0.0,
        )
        if top_share >= 0.7 and mention_count >= 3:
            spam_score = min(top_share, 1.0)

    components = {
        "mention_growth": (mention_growth, settings.narrative_score_mention_growth),
        "engagement_growth": (engagement_growth, settings.narrative_score_engagement_growth),
        "source_diversity": (source_diversity, settings.narrative_score_source_diversity),
        "recency": (recency, settings.narrative_score_recency),
        "acceleration": (acceleration, settings.narrative_score_acceleration),
        "persistence": (persistence, settings.narrative_score_persistence),
    }
    available = [(key, value, weight) for key, (value, weight) in components.items() if value is not None]
    total_weight = sum(weight for _, _, weight in available)
    completeness = total_weight
    weighted = 0.0
    if available and total_weight > 0:
        for _, value, weight in available:
            weighted += _clamp01(value) * weight
        score = 100.0 * (weighted / total_weight)
    else:
        score = 0.0
    score *= max(1.0 - spam_score * 0.4, 0.3)

    explanation = (
        f"{mention_count} stored mentions from {len(sources)} sources. "
        f"Recent window {len(recent)} vs previous {len(previous)}. "
        "Raw mention volume is not treated as organic popularity. "
        f"Spam indicator={spam_score:.2f}. "
        f"Completeness={completeness:.2f} (missing components are omitted, not zero-filled)."
    )
    confidence = completeness * (1.0 - spam_score * 0.5)
    return {
        "score": round(score, 2),
        "mention_count": mention_count,
        "mention_growth": mention_growth,
        "engagement_growth": engagement_growth,
        "independent_sources": len(sources),
        "independent_authors": len(authors) if authors else None,
        "source_diversity": source_diversity,
        "recency": recency,
        "acceleration": acceleration,
        "persistence": persistence,
        "spam_score": spam_score,
        "confidence": round(confidence, 4),
        "data_completeness": round(completeness, 4),
        "components": {
            key: None if value is None else round(float(value), 4)
            for key, (value, _) in components.items()
        },
        "explanation": explanation,
    }


def _upsert_narrative(
    session: Session,
    name: str,
    category: str | None,
    keywords: list[str],
    mentions: list[MentionRecord],
    now: datetime,
) -> Narrative:
    narrative = session.scalar(select(Narrative).where(Narrative.name == name))
    timestamps = [_aware(m.observed_at) for m in mentions if m.observed_at]
    first_seen = min(timestamps) if timestamps else now
    if narrative is None:
        narrative = Narrative(
            name=name[:128],
            category=category,
            description=f"Detected from stored news/social mentions of: {', '.join(keywords[:8])}",
            first_detected_at=first_seen,
            last_detected_at=now,
            trend_score=0.0,
        )
        session.add(narrative)
        session.flush()
    else:
        if first_seen < (_aware(narrative.first_detected_at) or first_seen):
            narrative.first_detected_at = first_seen
        narrative.category = category or narrative.category
    narrative.keywords = json.dumps(keywords)
    return narrative


def _match_topics(text: str) -> list[tuple[str, str, str]]:
    blob = text.lower()
    found: list[tuple[str, str, str]] = []
    for topic in TOPIC_CATALOG:
        name = str(topic["name"])
        category = str(topic["category"])
        for keyword in topic["keywords"]:  # type: ignore[attr-defined]
            needle = str(keyword)
            if needle in blob:
                found.append((name, category, needle))
                break
    return found


def _growth(current: int, previous: int, previous_empty: bool) -> float | None:
    if previous_empty and current == 0:
        return None
    if previous_empty:
        return None
    return (current - previous) / max(previous, 1)


def _engagement_total(mentions: list[MentionRecord]) -> int | None:
    values = [m.engagement for m in mentions if m.engagement is not None]
    if not values:
        return None
    return sum(values)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp01(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value
