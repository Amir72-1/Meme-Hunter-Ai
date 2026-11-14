"""Fetch and store public RSS news. Failed feeds are errors, not empty successes."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import IntelligenceScan, NewsItem, utc_now

logger = logging.getLogger("meme_hunter.news")

DEFAULT_RSS_FEEDS: tuple[dict[str, str], ...] = (
    {
        "name": "CoinDesk",
        "category": "cryptocurrency",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    },
    {
        "name": "Cointelegraph",
        "category": "cryptocurrency",
        "url": "https://cointelegraph.com/rss",
    },
    {
        "name": "The Verge AI",
        "category": "artificial_intelligence",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    },
    {
        "name": "BBC Technology",
        "category": "technology",
        "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    },
    {
        "name": "The Verge Games",
        "category": "gaming",
        "url": "https://www.theverge.com/rss/games/index.xml",
    },
    {
        "name": "BBC World",
        "category": "politics",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "name": "BBC Sport",
        "category": "sports",
        "url": "https://feeds.bbci.co.uk/sport/rss.xml",
    },
    {
        "name": "People Entertainment",
        "category": "celebrity",
        "url": "https://people.com/entertainment/feed/",
    },
    {
        "name": "The Verge",
        "category": "internet_culture",
        "url": "https://www.theverge.com/rss/index.xml",
    },
)

_TITLE_STRIP = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class ParsedArticle:
    title: str
    url: str
    source: str
    category: str
    feed_url: str
    published_at: datetime | None
    observed_at: datetime | None
    summary: str | None
    normalized_title: str


@dataclass
class NewsScanResult:
    status: str
    feeds_attempted: int = 0
    feeds_ok: int = 0
    items_fetched: int = 0
    items_saved: int = 0
    duplicates_skipped: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    skipped_feeds: list[dict[str, str]] = field(default_factory=list)


def normalize_title(title: str) -> str:
    lowered = html.unescape(title).lower().strip()
    lowered = _TITLE_STRIP.sub(" ", lowered)
    return _WHITESPACE.sub(" ", lowered).strip()


def parse_rss_xml(xml_text: str, source: str, category: str, feed_url: str) -> list[ParsedArticle]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid feed XML: {exc}") from exc

    articles: list[ParsedArticle] = []
    for node in root.iter():
        tag = _local_tag(node.tag)
        if tag not in {"item", "entry"}:
            continue
        article = _parse_entry(node, source, category, feed_url)
        if article is not None:
            articles.append(article)
    return articles


async def collect_news(
    session: Session,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
    feeds: list[dict[str, str]] | None = None,
) -> NewsScanResult:
    settings = settings or get_settings()
    result = NewsScanResult(status="running", started_at=datetime.now(timezone.utc))
    scan_row = IntelligenceScan(scan_type="news", started_at=result.started_at, status="running")
    session.add(scan_row)
    session.commit()

    selected_feeds = feeds or [dict(item) for item in DEFAULT_RSS_FEEDS]
    result.feeds_attempted = len(selected_feeds)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.news_timeout_seconds, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "MEME-HUNTER-AI/0.1 (paper-trading research)", "Accept": "application/rss+xml, application/xml, text/xml"},
        )

    try:
        fetched = await _fetch_all_feeds(client, selected_feeds, settings, result)
    finally:
        if owns_client:
            await client.aclose()

    existing_urls, existing_titles = _existing_keys(session)
    for article in fetched:
        if article.url in existing_urls or article.normalized_title in existing_titles:
            result.duplicates_skipped += 1
            continue
        recorded_at = utc_now()
        session.add(
            NewsItem(
                title=article.title[:512],
                source=article.source[:128],
                url=article.url[:1024],
                published_at=article.published_at,
                observed_at=article.observed_at or recorded_at,
                recorded_at=recorded_at,
                summary=article.summary,
                category=article.category,
                normalized_title=article.normalized_title[:512],
                feed_url=article.feed_url[:1024],
                created_at=recorded_at,
            )
        )
        existing_urls.add(article.url)
        existing_titles.add(article.normalized_title)
        result.items_saved += 1

    result.status = "completed" if result.feeds_ok or result.items_saved else ("failed" if result.errors else "completed")
    result.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = result.finished_at
    scan_row.status = result.status
    scan_row.items_fetched = result.items_fetched
    scan_row.items_saved = result.items_saved
    scan_row.duplicates_skipped = result.duplicates_skipped
    scan_row.details_json = json.dumps(
        {
            "feeds_attempted": result.feeds_attempted,
            "feeds_ok": result.feeds_ok,
            "skipped_feeds": result.skipped_feeds,
        }
    )
    scan_row.errors_json = json.dumps(result.errors)
    session.commit()
    logger.info(
        "News scan completed saved=%s duplicates=%s errors=%s",
        result.items_saved,
        result.duplicates_skipped,
        len(result.errors),
    )
    return result


async def _fetch_all_feeds(
    client: httpx.AsyncClient,
    feeds: list[dict[str, str]],
    settings: Settings,
    result: NewsScanResult,
) -> list[ParsedArticle]:
    semaphore = asyncio.Semaphore(5)
    articles: list[ParsedArticle] = []

    async def one(feed: dict[str, str]) -> None:
        async with semaphore:
            name = feed.get("name") or "unknown"
            category = feed.get("category") or "general"
            url = feed.get("url") or ""
            if not url:
                result.errors.append({"stage": "feed", "detail": f"{name}: missing url"})
                return
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("News feed unavailable %s: %s", name, exc)
                result.errors.append({"stage": "feed", "detail": f"{name}: {exc}"})
                result.skipped_feeds.append({"name": name, "reason": "unavailable"})
                return
            if response.status_code >= 400:
                result.errors.append(
                    {"stage": "feed", "detail": f"{name}: HTTP {response.status_code}"}
                )
                result.skipped_feeds.append({"name": name, "reason": f"http_{response.status_code}"})
                return
            try:
                parsed = parse_rss_xml(response.text, name, category, url)
            except ValueError as exc:
                result.errors.append({"stage": "parse", "detail": f"{name}: {exc}"})
                result.skipped_feeds.append({"name": name, "reason": "invalid_xml"})
                return
            result.feeds_ok += 1
            observed_at = datetime.now(timezone.utc)
            limited = parsed[: settings.news_max_items_per_feed]
            for article in limited:
                article.observed_at = observed_at
            result.items_fetched += len(limited)
            articles.extend(limited)

    await asyncio.gather(*(one(feed) for feed in feeds))
    return articles


def _existing_keys(session: Session) -> tuple[set[str], set[str]]:
    rows = session.execute(select(NewsItem.url, NewsItem.normalized_title)).all()
    urls = {row[0] for row in rows if row[0]}
    titles = {row[1] for row in rows if row[1]}
    return urls, titles


def _parse_entry(node: ElementTree.Element, source: str, category: str, feed_url: str) -> ParsedArticle | None:
    title = None
    link = None
    published = None
    summary = None
    for child in list(node):
        tag = _local_tag(child.tag)
        if tag == "title":
            title = _text(child)
        elif tag == "link":
            href = child.attrib.get("href")
            link = href or _text(child) or link
        elif tag in {"pubdate", "published", "updated", "date"}:
            published = _text(child) or published
        elif tag in {"description", "summary", "content"}:
            summary = _text(child) or summary
        elif tag == "guid" and not link:
            guid = _text(child)
            if guid and guid.startswith("http"):
                link = guid
    if not title or not link:
        return None
    return ParsedArticle(
        title=html.unescape(title).strip(),
        url=link.strip(),
        source=source,
        category=category,
        feed_url=feed_url,
        published_at=_parse_date(published),
        observed_at=None,
        summary=_clean_html(summary) if summary else None,
        normalized_title=normalize_title(title),
    )


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1].lower() if tag else ""


def _text(node: ElementTree.Element) -> str | None:
    chunks = [node.text or ""]
    chunks.extend("".join(child.itertext()) for child in list(node))
    value = " ".join(part for part in chunks if part).strip()
    return value or None


def _clean_html(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return _WHITESPACE.sub(" ", no_tags).strip()[:2000]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        iso = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if iso.tzinfo is None:
            return iso.replace(tzinfo=timezone.utc)
        return iso.astimezone(timezone.utc)
    except ValueError:
        return None
