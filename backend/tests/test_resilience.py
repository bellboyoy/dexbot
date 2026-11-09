"""Outage, failover and recovery behaviour of the full market data stack (real providers,
real HTTP client, mocked network)."""
import httpx
import pytest
import respx

from app.domain.chains import Chain
from app.domain.market import DataSource
from app.marketdata.aggregator import MarketDataService
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import ProviderUnavailable
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.http import CircuitBreaker, JsonHttpClient, RateLimiter

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"


def build(clock):
    fast = RateLimiter(10_000, 1, burst=1000)
    ds = JsonHttpClient("dexscreener", DS, limiters={"default": fast, "profiles": fast}, max_retries=1,
                        backoff_base_s=0.001, breaker=CircuitBreaker(2, 60, clock=clock))
    gt = JsonHttpClient("geckoterminal", GT, limiters={"default": fast}, max_retries=1,
                        backoff_base_s=0.001, breaker=CircuitBreaker(2, 60, clock=clock))
    return MarketDataService([DexScreenerProvider(ds), GeckoTerminalProvider(gt)])


@pytest.fixture
def pair_payload(load_fixture):
    data = load_fixture("dexscreener_pairs.json")
    assert data["pairs"][0]["pairAddress"] == POOL
    return data


@respx.mock
async def test_outage_failover_and_recovery(clock, pair_payload, load_fixture):
    ds_route = respx.get(f"{DS}/latest/dex/pairs/solana/{POOL}")
    respx.get(f"{GT}/networks/solana/pools/{POOL}").mock(
        return_value=httpx.Response(200, json=load_fixture("gecko_pool.json")))
    svc = build(clock)
    ds_client = svc.providers[0].client

    # 1. DexScreener down: every call still answers, from GeckoTerminal.
    ds_route.mock(return_value=httpx.Response(503))
    for _ in range(3):
        snap = await svc.get_pool(Chain.SOLANA, POOL)
        assert snap.source is DataSource.GECKOTERMINAL
        ds_client.cache._data.clear(); svc.providers[1].client.cache._data.clear()
    assert ds_client.breaker.state == "open"
    calls_while_open = ds_route.call_count
    await svc.get_pool(Chain.SOLANA, POOL)
    assert ds_route.call_count == calls_while_open          # open breaker: no traffic to the dead API

    # 2. DexScreener comes back, but breaker is still cooling down: keep using fallback.
    ds_route.mock(return_value=httpx.Response(200, json=pair_payload))
    svc.providers[1].client.cache._data.clear()
    assert (await svc.get_pool(Chain.SOLANA, POOL)).source is DataSource.GECKOTERMINAL

    # 3. After cooldown, a half-open probe succeeds and the primary is used again.
    clock.advance(60)
    snap = await svc.get_pool(Chain.SOLANA, POOL)
    assert snap.source is DataSource.DEXSCREENER and ds_client.breaker.state == "closed"
    await svc.aclose()


@respx.mock
async def test_failed_probe_reopens_breaker(clock):
    respx.get(f"{DS}/latest/dex/pairs/solana/{POOL}").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{GT}/networks/solana/pools/{POOL}").mock(side_effect=httpx.ConnectError("down"))
    svc = build(clock)
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            await svc.get_pool(Chain.SOLANA, POOL)
    clock.advance(60)
    with pytest.raises(ProviderUnavailable):           # probe fails
        await svc.get_pool(Chain.SOLANA, POOL)
    assert svc.providers[0].client.breaker.state == "open"
    assert {h["breaker_state"] for h in svc.health()} == {"open"}
    await svc.aclose()


@respx.mock
async def test_rate_limited_primary_falls_back(clock, load_fixture):
    respx.get(f"{DS}/latest/dex/pairs/solana/{POOL}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0.01"}))
    respx.get(f"{GT}/networks/solana/pools/{POOL}").mock(
        return_value=httpx.Response(200, json=load_fixture("gecko_pool.json")))
    svc = build(clock)
    snap = await svc.get_pool(Chain.SOLANA, POOL)
    assert snap.source is DataSource.GECKOTERMINAL
    assert svc.providers[0].client.status().rate_limited == 2
    await svc.aclose()


@respx.mock
async def test_pool_missing_on_primary_falls_through(clock, load_fixture):
    """A pool too new for DexScreener's index is still found on GeckoTerminal."""
    respx.get(f"{DS}/latest/dex/pairs/solana/{POOL}").mock(
        return_value=httpx.Response(200, json={"schemaVersion": "1.0.0", "pairs": None, "pair": None}))
    gt = respx.get(f"{GT}/networks/solana/pools/{POOL}")
    gt.mock(return_value=httpx.Response(200, json=load_fixture("gecko_pool.json")))
    svc = build(clock)
    assert (await svc.get_pool(Chain.SOLANA, POOL)).source is DataSource.GECKOTERMINAL
    gt.mock(return_value=httpx.Response(404))
    svc.providers[1].client.cache._data.clear()
    assert await svc.get_pool(Chain.SOLANA, POOL) is None      # not found anywhere -> None, not an error
    await svc.aclose()
