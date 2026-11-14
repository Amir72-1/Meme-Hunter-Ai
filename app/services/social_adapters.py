"""Modular social collectors. Unconfigured platforms return NOT_CONFIGURED."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import IntelligenceScan, SocialPost, utc_now

logger = logging.getLogger("meme_hunter.social")

NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class SocialPostDraft:
    platform: str
    external_id: str | None
    text: str
    source: str
    url: str
    published_at: datetime | None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    author_id: str | None = None


@dataclass
class AdapterStatus:
    platform: str
    status: str
    posts_fetched: int = 0
    detail: str | None = None


@dataclass
class SocialScanResult:
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    items_saved: int = 0
    duplicates_skipped: int = 0
    adapters: list[AdapterStatus] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


async def collect_social(
    session: Session,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SocialScanResult:
    settings = settings or get_settings()
    result = SocialScanResult(status="running", started_at=datetime.now(timezone.utc))
    scan_row = IntelligenceScan(scan_type="social", started_at=result.started_at, status="running")
    session.add(scan_row)
    session.commit()

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "MEME-HUNTER-AI/0.1 (paper-trading research)"},
        )
    drafts: list[SocialPostDraft] = []
    try:
        for collector in (_collect_reddit, _collect_x, _collect_telegram):
            adapter, posts = await collector(client, settings)
            result.adapters.append(adapter)
            if adapter.status == "error":
                result.errors.append(
                    {"stage": adapter.platform, "detail": adapter.detail or "error"}
                )
            drafts.extend(posts)
    finally:
        if owns_client:
            await client.aclose()

    existing_urls = {url for url in session.scalars(select(SocialPost.url)).all() if url}
    for draft in drafts:
        if draft.url in existing_urls:
            result.duplicates_skipped += 1
            continue
        engagement = _sum_optional(draft.likes, draft.comments, draft.shares)
        session.add(
            SocialPost(
                platform=draft.platform,
                external_id=draft.external_id,
                text=draft.text,
                source=draft.source,
                url=draft.url[:1024],
                published_at=draft.published_at,
                likes=draft.likes,
                comments=draft.comments,
                shares=draft.shares,
                views=draft.views,
                engagement=engagement,
                author_id=draft.author_id,
                created_at=utc_now(),
            )
        )
        existing_urls.add(draft.url)
        result.items_saved += 1

    configured = [item for item in result.adapters if item.status != NOT_CONFIGURED]
    if not configured:
        result.status = "completed"
    elif result.errors and result.items_saved == 0:
        result.status = "failed"
    else:
        result.status = "completed"
    result.finished_at = datetime.now(timezone.utc)
    scan_row.finished_at = result.finished_at
    scan_row.status = result.status
    scan_row.items_fetched = sum(item.posts_fetched for item in result.adapters)
    scan_row.items_saved = result.items_saved
    scan_row.duplicates_skipped = result.duplicates_skipped
    scan_row.details_json = json.dumps([item.__dict__ for item in result.adapters])
    scan_row.errors_json = json.dumps(result.errors)
    session.commit()
    return result


async def _collect_reddit(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[AdapterStatus, list[SocialPostDraft]]:
    if not (settings.reddit_client_id and settings.reddit_client_secret):
        return (
            AdapterStatus(
                platform="reddit",
                status=NOT_CONFIGURED,
                detail="Reddit API credentials are not configured.",
            ),
            [],
        )
    # Official Reddit OAuth collection is deferred until credentials are supplied
    # in a dedicated integration. Do not scrape anonymously and call it Reddit API.
    return (
        AdapterStatus(
            platform="reddit",
            status=NOT_CONFIGURED,
            detail="Reddit credentials are present but OAuth collection is not implemented yet.",
        ),
        [],
    )


async def _collect_x(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[AdapterStatus, list[SocialPostDraft]]:
    if not settings.x_bearer_token.strip():
        return (
            AdapterStatus(
                platform="x",
                status=NOT_CONFIGURED,
                detail="X API bearer token is not configured.",
            ),
            [],
        )
    headers = {"Authorization": f"Bearer {settings.x_bearer_token.strip()}"}
    try:
        response = await client.get(
            "https://api.x.com/2/tweets/search/recent",
            params={"query": "(meme OR solana) -is:retweet", "max_results": 10},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        return AdapterStatus(platform="x", status="error", detail=str(exc)), []
    if response.status_code >= 400:
        return (
            AdapterStatus(platform="x", status="error", detail=f"HTTP {response.status_code}"),
            [],
        )
    payload = response.json()
    posts: list[SocialPostDraft] = []
    for item in payload.get("data") or []:
        tweet_id = str(item.get("id") or "")
        text = item.get("text") or ""
        if not tweet_id or not text:
            continue
        posts.append(
            SocialPostDraft(
                platform="x",
                external_id=tweet_id,
                text=text,
                source="x",
                url=f"https://x.com/i/web/status/{tweet_id}",
                published_at=_parse_iso(item.get("created_at")),
                author_id=item.get("author_id"),
            )
        )
    return AdapterStatus(platform="x", status="ok", posts_fetched=len(posts)), posts


async def _collect_telegram(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[AdapterStatus, list[SocialPostDraft]]:
    if not settings.telegram_bot_token.strip():
        return (
            AdapterStatus(
                platform="telegram",
                status=NOT_CONFIGURED,
                detail="Telegram bot token is not configured.",
            ),
            [],
        )
    if not settings.telegram_public_channels.strip():
        return (
            AdapterStatus(
                platform="telegram",
                status=NOT_CONFIGURED,
                detail="No permitted public Telegram channels are configured.",
            ),
            [],
        )
    return (
        AdapterStatus(
            platform="telegram",
            status=NOT_CONFIGURED,
            detail="Telegram public-channel collection is not implemented yet.",
        ),
        [],
    )


def _sum_optional(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
