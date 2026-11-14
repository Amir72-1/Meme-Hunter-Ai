"""Mock DexScreener payloads and HTTPX transport. No live network."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.services.dexscreener_client import DexScreenerClient

TOKEN_A = "TokenA111111111111111111111111111111111111111"
TOKEN_B = "TokenB222222222222222222222222222222222222222"
ETH_TOKEN = "0x1111111111111111111111111111111111111111"


def run(coro):
    return asyncio.run(coro)


def recent_created_at_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def pair_payload(address: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": f"Pair{address[:6]}",
        "baseToken": {"address": address, "name": "Test Meme", "symbol": "MEME"},
        "quoteToken": {
            "address": "So11111111111111111111111111111111111111112",
            "name": "SOL",
            "symbol": "SOL",
        },
        "priceUsd": "0.02",
        "marketCap": 25000,
        "fdv": 40000,
        "liquidity": {"usd": 20000},
        "volume": {"m5": 100.0, "h1": 800.0, "h24": 9000.0},
        "priceChange": {"m5": 1.1, "h1": 2.2, "h24": 3.3},
        "txns": {
            "m5": {"buys": 3, "sells": 1},
            "h1": {"buys": 12, "sells": 5},
            "h24": {"buys": 40, "sells": 20},
        },
        "pairCreatedAt": recent_created_at_ms(),
        "boosts": {"active": 2},
    }
    payload.update(overrides)
    return payload


def profile(address: str, chain_id: str = "solana") -> dict[str, Any]:
    return {"chainId": chain_id, "tokenAddress": address, "url": "https://example.com"}


def boost(address: str, chain_id: str = "solana", amount: int = 5) -> dict[str, Any]:
    return {
        "chainId": chain_id,
        "tokenAddress": address,
        "amount": amount,
        "totalAmount": amount,
    }


class ScriptedDex:
    def __init__(self) -> None:
        self.profiles: list[Any] | httpx.Response = []
        self.latest_boosts: list[Any] | httpx.Response = []
        self.top_boosts: list[Any] | httpx.Response = []
        self.tokens: list[Any] | httpx.Response = []
        self.token_pairs: dict[str, list[Any] | httpx.Response] = {}
        self.calls: list[str] = []
        self._rate_limit_remaining = 0

    def fail_once_rate_limit(self, times: int = 1) -> None:
        self._rate_limit_remaining = times

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self._rate_limit_remaining > 0:
            self._rate_limit_remaining -= 1
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate"})
        if path.endswith("/token-profiles/latest/v1"):
            return self._as_response(self.profiles)
        if path.endswith("/token-boosts/latest/v1"):
            return self._as_response(self.latest_boosts)
        if path.endswith("/token-boosts/top/v1"):
            return self._as_response(self.top_boosts)
        if "/tokens/v1/solana/" in path:
            return self._as_response(self.tokens)
        if "/token-pairs/v1/solana/" in path:
            address = path.rsplit("/", 1)[-1]
            return self._as_response(self.token_pairs.get(address, []))
        return httpx.Response(404, json={"error": "not mocked"})

    def _as_response(self, value: list[Any] | httpx.Response) -> httpx.Response:
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value)

    def client(self) -> DexScreenerClient:
        return DexScreenerClient(
            settings=get_settings(),
            transport=httpx.MockTransport(self.handler),
            rate_limit=False,
        )

    def ctor(self) -> Callable[..., DexScreenerClient]:
        def _ctor(*args: Any, **kwargs: Any) -> DexScreenerClient:
            return self.client()

        return _ctor
