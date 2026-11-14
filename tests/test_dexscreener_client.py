"""DexScreener client tests against mocked HTTPX responses."""

from __future__ import annotations

import httpx
import pytest

from app.services.dexscreener_client import DexScreenerError
from tests.dexscreener_fakes import (
    ETH_TOKEN,
    TOKEN_A,
    TOKEN_B,
    boost,
    pair_payload,
    profile,
    run,
    ScriptedDex,
)
from app.services.market_data import extract_solana_addresses, select_best_pair


def test_profile_retrieval() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A), profile(ETH_TOKEN, chain_id="ethereum")]

    async def body() -> None:
        async with script.client() as client:
            items = await client.get_latest_profiles()
        assert len(items) == 2
        assert items[0]["tokenAddress"] == TOKEN_A

    run(body())


def test_boost_retrieval() -> None:
    script = ScriptedDex()
    script.latest_boosts = [boost(TOKEN_A, amount=4)]
    script.top_boosts = [boost(TOKEN_B, amount=9)]

    async def body() -> None:
        async with script.client() as client:
            latest = await client.get_latest_boosts()
            top = await client.get_top_boosts()
        assert latest[0]["totalAmount"] == 4
        assert top[0]["tokenAddress"] == TOKEN_B

    run(body())


def test_solana_only_filtering() -> None:
    discovered = extract_solana_addresses(
        [profile(TOKEN_A), profile(ETH_TOKEN, chain_id="ethereum")],
        [boost(ETH_TOKEN, chain_id="ethereum")],
        [boost(TOKEN_B)],
    )
    assert set(discovered) == {TOKEN_A, TOKEN_B}


def test_market_pair_parsing_and_missing_fields() -> None:
    full = pair_payload(TOKEN_A)
    sparse = {"chainId": "solana", "baseToken": {"address": TOKEN_A}, "quoteToken": {}}
    best = select_best_pair([full, sparse], TOKEN_A)
    assert best is not None
    assert best["pairAddress"] == full["pairAddress"]
    assert sparse.get("priceUsd") is None


def test_api_failure_is_not_empty_success() -> None:
    script = ScriptedDex()
    script.profiles = httpx.Response(500, json={"error": "nope"})

    async def body() -> None:
        async with script.client() as client:
            with pytest.raises(DexScreenerError):
                await client.get_latest_profiles()

    run(body())


def test_rate_limit_retry_then_success() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.fail_once_rate_limit(1)

    async def body() -> None:
        async with script.client() as client:
            items = await client.get_latest_profiles()
        assert items[0]["tokenAddress"] == TOKEN_A

    run(body())


def test_rate_limit_exhausted_raises() -> None:
    script = ScriptedDex()
    script.profiles = [profile(TOKEN_A)]
    script.fail_once_rate_limit(10)

    async def body() -> None:
        async with script.client() as client:
            with pytest.raises(DexScreenerError) as exc:
                await client.get_latest_profiles()
        assert exc.value.status_code == 429

    run(body())
