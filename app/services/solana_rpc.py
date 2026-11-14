"""Public Solana JSON-RPC client. Failed calls are errors, not empty successes."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.services.rpc_accounting import (
    KIND_CLIENT,
    KIND_COOLDOWN_ACTIVE,
    KIND_PROVIDER,
    KIND_RATE_LIMITED,
    KIND_RETRYABLE,
    KIND_TIMEOUT,
    RpcAccounting,
)
from app.services.rpc_cooldown import RpcCooldownStore, compute_cooldown_seconds, get_cooldown_store
from app.timeutil import utc_now

logger = logging.getLogger("meme_hunter.solana_rpc")

RETRYABLE = {408, 429, 500, 502, 503, 504}


class SolanaRpcError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        kind: str = KIND_PROVIDER,
        retry_after: float | None = None,
        accounting: RpcAccounting | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.retry_after = retry_after
        self.accounting = accounting

    @property
    def is_rate_limited(self) -> bool:
        return self.kind == KIND_RATE_LIMITED or self.status_code == 429


@dataclass
class RpcAccountResult:
    value: dict[str, Any] | None
    slot: int | None = None
    observed_at: datetime | None = None
    from_cache: bool = False


@dataclass
class RpcListResult:
    value: list[dict[str, Any]]
    slot: int | None = None
    observed_at: datetime | None = None
    from_cache: bool = False


@dataclass
class RpcCallResult:
    result: Any
    observed_at: datetime
    from_cache: bool = False


class SolanaRpcClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        cache_ttl_seconds: float = 45.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any, datetime]] = {}
        self._cache_ttl = cache_ttl_seconds
        self._request_id = 0
        self.accounting = RpcAccounting()
        self.last_attempt_at: datetime | None = None
        self.cooldown_store: RpcCooldownStore = get_cooldown_store()
        self._cooldown_until: datetime | None = None

    async def __aenter__(self) -> SolanaRpcClient:
        kwargs: dict[str, Any] = {
            "base_url": self.settings.solana_rpc_url.rstrip("/"),
            "timeout": httpx.Timeout(self.settings.solana_rpc_timeout_seconds, connect=5.0),
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "MEME-HUNTER-AI/0.1 (paper-trading research)",
            },
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def is_open(self) -> bool:
        return self._client is not None

    async def get_parsed_account(self, address: str) -> dict[str, Any] | None:
        account = await self.get_account_info(address, encoding="jsonParsed")
        return account.value

    async def get_account_info(self, address: str, encoding: str = "base64") -> RpcAccountResult:
        call = await self.rpc("getAccountInfo", [address, {"encoding": encoding}])
        result = call.result
        if not isinstance(result, dict):
            raise SolanaRpcError("Malformed getAccountInfo result")
        slot = None
        context = result.get("context")
        if isinstance(context, dict) and context.get("slot") is not None:
            try:
                slot = int(context["slot"])
            except (TypeError, ValueError):
                slot = None
        value = result.get("value")
        if value is not None and not isinstance(value, dict):
            raise SolanaRpcError("getAccountInfo value is not an object")
        return RpcAccountResult(
            value=value,
            slot=slot,
            observed_at=call.observed_at,
            from_cache=call.from_cache,
        )

    async def get_token_supply(self, mint: str) -> dict[str, Any] | None:
        call = await self.rpc("getTokenSupply", [mint])
        result = call.result
        if not isinstance(result, dict):
            raise SolanaRpcError("Malformed getTokenSupply result")
        return result.get("value")

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = await self.get_token_largest_accounts_result(mint)
        return result.value

    async def get_token_largest_accounts_result(self, mint: str) -> RpcListResult:
        call = await self.rpc("getTokenLargestAccounts", [mint])
        result = call.result
        if not isinstance(result, dict):
            raise SolanaRpcError("Malformed getTokenLargestAccounts result")
        slot = None
        context = result.get("context")
        if isinstance(context, dict) and context.get("slot") is not None:
            try:
                slot = int(context["slot"])
            except (TypeError, ValueError):
                slot = None
        value = result.get("value")
        if value is None:
            accounts: list[dict[str, Any]] = []
        elif not isinstance(value, list):
            raise SolanaRpcError("getTokenLargestAccounts value is not a list")
        else:
            accounts = [item for item in value if isinstance(item, dict)]
        return RpcListResult(
            value=accounts,
            slot=slot,
            observed_at=call.observed_at,
            from_cache=call.from_cache,
        )

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 1000,
        before: str | None = None,
    ) -> RpcCallResult:
        options: dict[str, Any] = {"limit": min(max(int(limit), 1), 1000)}
        if before:
            options["before"] = before
        return await self.rpc("getSignaturesForAddress", [address, options])

    async def get_transaction(self, signature: str, encoding: str = "jsonParsed") -> RpcCallResult:
        return await self.rpc(
            "getTransaction",
            [signature, {"encoding": encoding, "maxSupportedTransactionVersion": 0}],
        )

    def cooldown_active(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        stored = self.cooldown_store.active(now=now) if self.cooldown_store is not None else None
        local_active = self._cooldown_until is not None and now < self._cooldown_until
        return stored is not None or local_active

    def halt_state(self) -> tuple[bool, str | None]:
        if self.cooldown_active():
            return True, KIND_COOLDOWN_ACTIVE
        if self.accounting.stop_reason == KIND_RATE_LIMITED:
            return True, KIND_RATE_LIMITED
        return False, None

    def resume_after_cooldown(self) -> None:
        """Allow a new evidence lease after the persisted cooldown has expired."""
        if self.cooldown_active():
            return
        self._cooldown_until = None
        if self.accounting.stop_reason == KIND_RATE_LIMITED:
            self.accounting.stop_reason = None
            self.accounting.consecutive_429 = 0

    @property
    def should_halt(self) -> bool:
        blocked, _reason = self.halt_state()
        return blocked

    @property
    def halt_reason(self) -> str | None:
        _blocked, reason = self.halt_state()
        return reason

    @property
    def batch_should_stop(self) -> bool:
        return self.should_halt

    async def rpc(self, method: str, params: list[Any]) -> RpcCallResult:
        blocked, reason = self.halt_state()
        if blocked:
            raise SolanaRpcError(
                "Solana RPC cooldown is active. No HTTP request was sent.",
                status_code=429,
                kind=reason or KIND_COOLDOWN_ACTIVE,
                accounting=self.accounting.copy(),
            )
        cache_key = f"{method}:{json.dumps(params, sort_keys=True)}"
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self._cache_ttl:
            self.accounting.cache_hits += 1
            return RpcCallResult(result=cached[1], observed_at=cached[2], from_cache=True)

        if self._client is None:
            raise SolanaRpcError("Solana RPC client is not started", kind=KIND_CLIENT)

        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        retries = max(0, int(self.settings.solana_rpc_max_retries))
        last_error: SolanaRpcError | None = None
        for attempt in range(retries + 1):
            self.last_attempt_at = utc_now()
            self.accounting.attempts += 1
            self.accounting.bump_method(method, "attempts")
            if attempt > 0:
                self.accounting.retries += 1
                self.accounting.bump_method(method, "retries")
            try:
                response = await self._client.post("", json=payload)
            except httpx.TimeoutException:
                last_error = self._record_failure(
                    method,
                    SolanaRpcError(f"Timeout calling {method}", status_code=408, kind=KIND_TIMEOUT),
                    timeout=True,
                )
                await self._backoff(attempt, retries, retry_after=None)
                continue
            except httpx.HTTPError as exc:
                last_error = self._record_failure(
                    method,
                    SolanaRpcError(f"HTTP error calling {method}: {exc}", kind=KIND_RETRYABLE),
                )
                await self._backoff(attempt, retries, retry_after=None)
                continue
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_error = self._record_failure(
                    method,
                    SolanaRpcError(
                        f"Rate limited calling {method}",
                        status_code=429,
                        kind=KIND_RATE_LIMITED,
                        retry_after=retry_after,
                    ),
                    rate_limited=True,
                )
                await self._backoff(attempt, retries, retry_after=retry_after)
                continue
            if response.status_code in RETRYABLE:
                last_error = self._record_failure(
                    method,
                    SolanaRpcError(
                        f"Retryable HTTP {response.status_code} for {method}",
                        status_code=response.status_code,
                        kind=KIND_RETRYABLE,
                    ),
                )
                await self._backoff(attempt, retries, retry_after=None)
                continue
            if response.status_code >= 400:
                error = self._record_failure(
                    method,
                    SolanaRpcError(
                        f"Solana RPC HTTP {response.status_code} for {method}",
                        status_code=response.status_code,
                        kind=KIND_PROVIDER,
                    ),
                )
                raise error
            try:
                body = response.json()
            except ValueError as exc:
                error = self._record_failure(
                    method,
                    SolanaRpcError(f"Malformed JSON from {method}", kind=KIND_PROVIDER),
                )
                raise error from exc
            if not isinstance(body, dict):
                raise self._record_failure(
                    method,
                    SolanaRpcError(f"Unexpected RPC envelope from {method}", kind=KIND_PROVIDER),
                )
            if body.get("error"):
                raise self._record_failure(
                    method,
                    SolanaRpcError(f"{method} error: {body['error']}", kind=KIND_PROVIDER),
                )
            self.accounting.successes += 1
            self.accounting.bump_method(method, "successes")
            self.accounting.consecutive_429 = 0
            observed_at = utc_now()
            result = body.get("result")
            self._cache[cache_key] = (time.monotonic(), result, observed_at)
            return RpcCallResult(result=result, observed_at=observed_at, from_cache=False)
        if last_error is not None and last_error.is_rate_limited:
            self._mark_rate_limit_stop(retry_after=last_error.retry_after)
        raise last_error or SolanaRpcError(f"Failed calling {method}", kind=KIND_PROVIDER)

    def _record_failure(
        self,
        method: str,
        error: SolanaRpcError,
        *,
        rate_limited: bool = False,
        timeout: bool = False,
    ) -> SolanaRpcError:
        self.accounting.failures += 1
        self.accounting.bump_method(method, "failures")
        if rate_limited:
            self.accounting.http_429 += 1
            self.accounting.bump_method(method, "http_429")
            self.accounting.consecutive_429 += 1
        elif timeout:
            self.accounting.timeouts += 1
            self.accounting.bump_method(method, "timeouts")
        else:
            self.accounting.other_errors += 1
        error.accounting = self.accounting.copy()
        return error

    def _mark_rate_limit_stop(self, retry_after: float | None = None) -> None:
        threshold = max(1, int(self.settings.solana_rpc_stop_after_429))
        if self.accounting.consecutive_429 < threshold:
            return
        self.accounting.stop_reason = KIND_RATE_LIMITED
        seconds = compute_cooldown_seconds(
            self.settings,
            retry_after,
            self.accounting.consecutive_429,
        )
        until = utc_now() + timedelta(seconds=seconds)
        self._cooldown_until = until
        if self.cooldown_store is not None:
            self.cooldown_store.activate(
                until_at=until,
                reason=KIND_RATE_LIMITED,
                retry_after_seconds=retry_after,
                backoff_seconds=seconds,
                consecutive_429=self.accounting.consecutive_429,
            )

    async def _backoff(
        self,
        attempt: int,
        retries: int,
        *,
        retry_after: float | None,
    ) -> None:
        if attempt >= retries:
            return
        cap = max(0.0, float(self.settings.solana_rpc_max_backoff_seconds))
        wait = min(2 ** attempt, cap) if cap else 0.0
        if retry_after is not None:
            wait = min(retry_after, cap) if cap else retry_after
        jitter = random.uniform(0.5, 1.0)
        await asyncio.sleep(max(0.0, wait * jitter))


def _parse_retry_after(value: str | None) -> float | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
