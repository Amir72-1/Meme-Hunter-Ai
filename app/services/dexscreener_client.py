"""Asynchronous DexScreener HTTP client. Failed requests are never empty successes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger("meme_hunter.dexscreener")

SOLANA_CHAIN_ID = "solana"
PROFILE_LIMIT_PER_MINUTE = 60
PAIR_LIMIT_PER_MINUTE = 300
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class DexScreenerError(Exception):
    """Raised when DexScreener cannot be used as a successful data source."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimiter:
    def __init__(self, max_per_minute: int, enabled: bool = True) -> None:
        self.max_per_minute = max_per_minute
        self.enabled = enabled
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._timestamps = [stamp for stamp in self._timestamps if now - stamp < 60]
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return
                wait_for = 60 - (now - self._timestamps[0])
                logger.warning("DexScreener rate limit pause for %.2fs", wait_for)
                await asyncio.sleep(max(wait_for, 0.05))


class DexScreenerClient:
    """HTTPX client for documented DexScreener REST endpoints."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        rate_limit: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._profile_limiter = RateLimiter(PROFILE_LIMIT_PER_MINUTE, enabled=rate_limit)
        self._pair_limiter = RateLimiter(PAIR_LIMIT_PER_MINUTE, enabled=rate_limit)

    async def __aenter__(self) -> DexScreenerClient:
        timeout = httpx.Timeout(self.settings.dexscreener_timeout_seconds, connect=5.0)
        kwargs: dict[str, Any] = {
            "base_url": self.settings.dexscreener_base_url.rstrip("/"),
            "timeout": timeout,
            "headers": {
                "Accept": "application/json",
                "User-Agent": "MEME-HUNTER-AI/0.1 (paper-trading research)",
            },
            "follow_redirects": True,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise DexScreenerError("DexScreener client is not started")
        return self._client

    async def get_latest_profiles(self) -> list[dict[str, Any]]:
        return await self._get_list("/token-profiles/latest/v1", limiter=self._profile_limiter)

    async def get_latest_boosts(self) -> list[dict[str, Any]]:
        return await self._get_list("/token-boosts/latest/v1", limiter=self._profile_limiter)

    async def get_top_boosts(self) -> list[dict[str, Any]]:
        return await self._get_list("/token-boosts/top/v1", limiter=self._profile_limiter)

    async def get_token_pairs(self, token_address: str) -> list[dict[str, Any]]:
        path = f"/token-pairs/v1/{SOLANA_CHAIN_ID}/{token_address}"
        return await self._get_list(path, limiter=self._pair_limiter)

    async def get_tokens(self, token_addresses: list[str]) -> list[dict[str, Any]]:
        if not token_addresses:
            return []
        batch_size = max(1, self.settings.dexscreener_token_batch_size)
        pairs: list[dict[str, Any]] = []
        for index in range(0, len(token_addresses), batch_size):
            chunk = token_addresses[index : index + batch_size]
            joined = ",".join(chunk)
            path = f"/tokens/v1/{SOLANA_CHAIN_ID}/{joined}"
            pairs.extend(await self._get_list(path, limiter=self._pair_limiter))
        return pairs

    async def _get_list(self, path: str, limiter: RateLimiter) -> list[dict[str, Any]]:
        payload = await self._request_json(path, limiter)
        return _coerce_item_list(payload, path)

    async def _request_json(self, path: str, limiter: RateLimiter) -> Any:
        client = self._require_client()
        retries = max(0, self.settings.dexscreener_max_retries)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            await limiter.acquire()
            try:
                response = await asyncio.wait_for(
                    client.get(path),
                    timeout=self.settings.dexscreener_request_deadline_seconds,
                )
            except asyncio.TimeoutError:
                last_error = DexScreenerError(
                    f"Deadline exceeded requesting {path} (HTTP/DNS bounded by "
                    f"{self.settings.dexscreener_request_deadline_seconds}s)"
                )
                logger.warning("DexScreener request deadline on %s (attempt %s)", path, attempt + 1)
                await _backoff(attempt, limiter.enabled)
                continue
            except httpx.TimeoutException:
                last_error = DexScreenerError(f"Timeout requesting {path}")
                logger.warning("DexScreener timeout on %s (attempt %s)", path, attempt + 1)
                await _backoff(attempt, limiter.enabled)
                continue
            except httpx.HTTPError as exc:
                last_error = DexScreenerError(f"HTTP error requesting {path}: {exc}")
                logger.warning("DexScreener HTTP error on %s: %s", path, exc)
                await _backoff(attempt, limiter.enabled)
                continue

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                logger.warning(
                    "DexScreener rate-limited %s; waiting %.2fs (attempt %s)",
                    path,
                    retry_after,
                    attempt + 1,
                )
                last_error = DexScreenerError(
                    f"Rate limited requesting {path}",
                    status_code=429,
                )
                await asyncio.sleep(retry_after)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = DexScreenerError(
                    f"Retryable status {response.status_code} for {path}",
                    status_code=response.status_code,
                )
                logger.warning(
                    "DexScreener retryable status %s on %s",
                    response.status_code,
                    path,
                )
                await _backoff(attempt, limiter.enabled)
                continue

            if response.status_code >= 400:
                raise DexScreenerError(
                    f"DexScreener returned HTTP {response.status_code} for {path}",
                    status_code=response.status_code,
                )

            try:
                return response.json()
            except ValueError as exc:
                raise DexScreenerError(f"Malformed JSON from {path}") from exc

        raise last_error or DexScreenerError(f"Failed requesting {path}")


def _coerce_item_list(payload: Any, path: str) -> list[dict[str, Any]]:
    if payload is None:
        raise DexScreenerError(f"Empty JSON payload from {path}")
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("pairs"), list):
            items = payload["pairs"]
        else:
            items = [payload]
    else:
        raise DexScreenerError(f"Unexpected JSON type from {path}: {type(payload).__name__}")

    objects: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            objects.append(item)
        else:
            logger.warning("Skipping malformed DexScreener item on %s: %r", path, item)
    return objects


def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 0.1), 60.0)
        except ValueError:
            pass
    return 1.5


async def _backoff(attempt: int, enabled: bool = True) -> None:
    if not enabled:
        return
    await asyncio.sleep(min(2 ** attempt, 8))
