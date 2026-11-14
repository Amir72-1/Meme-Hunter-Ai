"""RSS news collector tests with mocked HTTP."""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import delete, select

from app.database import get_session_factory
from app.models import IntelligenceScan, NewsItem
from app.services.news_collector import collect_news, normalize_title, parse_rss_xml

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>OpenAI announces a new model</title>
  <link>https://example.com/ai-1</link>
  <pubDate>Sun, 01 Nov 2026 12:00:00 GMT</pubDate>
  <description>Researchers describe an AI breakthrough.</description>
</item>
<item>
  <title>OpenAI announces a new model</title>
  <link>https://example.com/ai-1-dup</link>
  <pubDate>Sun, 01 Nov 2026 12:05:00 GMT</pubDate>
  <description>Duplicate title should be skipped after the first save.</description>
</item>
</channel></rss>
"""

INVALID = "<not-rss>"


def run(coro):
    return asyncio.run(coro)


def test_parse_rss_and_normalize() -> None:
    articles = parse_rss_xml(RSS, "TestFeed", "artificial_intelligence", "https://example.com/rss")
    assert len(articles) == 2
    assert articles[0].url == "https://example.com/ai-1"
    assert articles[0].published_at is not None
    assert normalize_title("OpenAI announces a new model") == articles[0].normalized_title


def test_collect_news_dedupes_and_skips_invalid_feeds() -> None:
    session = get_session_factory()()
    session.execute(delete(NewsItem))
    session.execute(delete(IntelligenceScan))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/good.xml"):
            return httpx.Response(200, text=RSS)
        if str(request.url).endswith("/bad.xml"):
            return httpx.Response(200, text=INVALID)
        if str(request.url).endswith("/down.xml"):
            return httpx.Response(503, text="nope")
        return httpx.Response(404)

    feeds = [
        {"name": "Good", "category": "artificial_intelligence", "url": "https://example.com/good.xml"},
        {"name": "Bad", "category": "technology", "url": "https://example.com/bad.xml"},
        {"name": "Down", "category": "sports", "url": "https://example.com/down.xml"},
    ]

    async def body():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_news(session, client=client, feeds=feeds)

    result = run(body())
    assert result.feeds_ok == 1
    assert result.items_saved == 1
    assert result.duplicates_skipped == 1
    assert any("invalid" in error["detail"].lower() or "Invalid" in error["detail"] for error in result.errors)
    rows = list(session.scalars(select(NewsItem)).all())
    assert len(rows) == 1
    assert rows[0].source == "Good"
    assert rows[0].published_at is not None
    assert rows[0].observed_at is not None
    assert rows[0].recorded_at is not None
    assert rows[0].observed_at >= rows[0].published_at
    assert rows[0].recorded_at >= rows[0].observed_at
    assert rows[0].feed_url == "https://example.com/good.xml"
    session.close()


NO_DATE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Timestamp missing on purpose</title>
  <link>https://example.com/no-date</link>
  <description>Publication time unknown.</description>
</item>
<item>
  <title>Invalid publication stamp</title>
  <link>https://example.com/bad-date</link>
  <pubDate>not-a-date</pubDate>
</item>
</channel></rss>
"""


def test_missing_rss_published_at_stays_null() -> None:
    session = get_session_factory()()
    session.execute(delete(NewsItem))
    session.execute(delete(IntelligenceScan))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NO_DATE_RSS)

    feeds = [{"name": "Sparse", "category": "technology", "url": "https://example.com/sparse.xml"}]

    async def body():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_news(session, client=client, feeds=feeds)

    result = run(body())
    assert result.items_saved == 2
    rows = list(session.scalars(select(NewsItem).order_by(NewsItem.id.asc())).all())
    assert [row.published_at for row in rows] == [None, None]
    assert all(row.observed_at is not None for row in rows)
    assert all(row.recorded_at is not None for row in rows)
    assert all(row.source == "Sparse" for row in rows)
    session.close()
