from decimal import Decimal

import httpx
import pytest
import respx

from app.domain.chains import Chain
from app.domain.market import DataSource
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import CapabilityNotSupported, InvalidResponse, UnsupportedPoolId
from app.marketdata.http import JsonHttpClient, RateLimiter

BASE = "https://api.dexscreener.com"


def provider(chains=None):
    rl = RateLimiter(10_000, 1, burst=1000)
    client = JsonHttpClient("dexscreener", BASE, limiters={"default": rl, "profiles": rl}, backoff_base_s=0.001)
    return DexScreenerProvider(client, chains)


def test_parse_real_pair(load_fixture):
    p = load_fixture("dexscreener_tokens_v1.json")[0]
    snap = provider().parse_pair(p)
    assert snap.source is DataSource.DEXSCREENER and snap.chain is Chain.SOLANA
    assert snap.pool_address == p["pairAddress"]
    assert snap.price_usd == Decimal(p["priceUsd"])
    assert snap.liquidity_usd == Decimal(str(p["liquidity"]["usd"]))
    assert snap.window("h1").buys == p["txns"]["h1"]["buys"]
    assert snap.window("m5").volume_usd == Decimal(str(p["volume"]["m5"]))
    assert snap.window("h1").buyers is None          # DexScreener has no unique-trader counts
    assert snap.window("m15").buys is None           # window DexScreener does not provide
    assert snap.pool_created_at is not None


def test_parse_accepts_32_byte_pool_id():
    token = "0x" + "cd" * 20
    quote = "0x" + "11" * 20
    snap = provider().parse_pair({
        "chainId": "base", "pairAddress": "0x" + "AB" * 32,
        "baseToken": {"address": token, "symbol": "AAA", "name": "Aaa"},
        "quoteToken": {"address": quote, "symbol": "WETH"},
        "priceUsd": "1.2",
    })
    assert snap is not None
    assert snap.pool_address == "0x" + "ab" * 32
    assert snap.base_token.address == token
    assert provider().parse_pair({
        "chainId": "base", "pairAddress": "0x" + "ab" * 32,
        "baseToken": {"address": "0x" + "ef" * 32, "symbol": "NO"},
        "priceUsd": "1",
    }) is None


def test_null_fields_stay_unknown(load_fixture):
    p = dict(load_fixture("dexscreener_tokens_v1.json")[0], fdv=None, marketCap=None, liquidity=None)
    snap = provider().parse_pair(p)
    assert snap.fdv_usd is None and snap.market_cap_usd is None and snap.liquidity_usd is None


def test_unsupported_chain_and_bad_address_dropped(load_fixture):
    p = load_fixture("dexscreener_tokens_v1.json")[0]
    assert provider().parse_pair(dict(p, chainId="robinhood")) is None
    assert provider([Chain.BASE]).parse_pair(p) is None           # chain disabled
    assert provider().parse_pair(dict(p, pairAddress="0xnotsolana")) is None


def test_non_object_pair_raises():
    with pytest.raises(InvalidResponse):
        provider().parse_pair(["nope"])


@respx.mock
async def test_get_pool(load_fixture):
    data = load_fixture("dexscreener_pairs.json")
    addr = data["pairs"][0]["pairAddress"]
    respx.get(f"{BASE}/latest/dex/pairs/solana/{addr}").mock(return_value=httpx.Response(200, json=data))
    snap = await provider().get_pool(Chain.SOLANA, addr)
    assert snap is not None and snap.pool_address == addr


@respx.mock
async def test_32_byte_pool_http_400_is_unsupported():
    pool_id = "0x" + "ab" * 32
    respx.get(f"{BASE}/latest/dex/pairs/base/{pool_id}").mock(return_value=httpx.Response(400, text="nope"))
    with pytest.raises(UnsupportedPoolId) as caught:
        await provider().get_pool(Chain.BASE, pool_id)
    assert caught.value.reason == "unsupported_pool_id"


@respx.mock
async def test_get_pool_not_found():
    addr = "So11111111111111111111111111111111111111112"
    respx.get(f"{BASE}/latest/dex/pairs/solana/{addr}").mock(
        return_value=httpx.Response(200, json={"schemaVersion": "1.0.0", "pairs": None, "pair": None}))
    assert await provider().get_pool(Chain.SOLANA, addr) is None


@respx.mock
async def test_get_tokens_batches():
    route = respx.get(url__regex=rf"{BASE}/tokens/v1/base/.*").mock(return_value=httpx.Response(200, json=[]))
    addrs = [f"0x{i:040x}" for i in range(65)]
    assert await provider().get_tokens(Chain.BASE, addrs) == []
    assert route.call_count == 3


@respx.mock
async def test_discover_filters_chains(load_fixture):
    profiles = load_fixture("dexscreener_token_profiles.json")
    profiles.append({"chainId": "base", "tokenAddress": "not-an-address"})
    respx.get(f"{BASE}/token-profiles/latest/v1").mock(return_value=httpx.Response(200, json=profiles))
    respx.get(f"{BASE}/token-boosts/latest/v1").mock(return_value=httpx.Response(200, json=[
        {"chainId": "bsc", "tokenAddress": "0x" + "ab" * 20}]))
    leads = await provider().discover([Chain.SOLANA, Chain.BASE, Chain.BSC])
    supported = {"solana", "base", "bsc"}
    expected = sum(1 for p in profiles[:-1] if p["chainId"] in supported) + 1
    assert len(leads) == expected
    assert any(l.reason == "dexscreener_boost_paid" for l in leads)


async def test_unsupported_capabilities():
    with pytest.raises(CapabilityNotSupported):
        await provider().get_ohlcv(Chain.SOLANA, "x", None)
