from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from app.domain.chains import Chain
from app.domain.market import Timeframe, TradeKind
from app.marketdata.errors import InvalidResponse
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.http import JsonHttpClient, RateLimiter

BASE = "https://api.geckoterminal.com/api/v2"


def provider():
    client = JsonHttpClient("geckoterminal", BASE, limiters={"default": RateLimiter(10_000, 1, burst=1000)},
                            backoff_base_s=0.001)
    return GeckoTerminalProvider(client)


def test_parse_new_pool_with_unique_buyers(load_fixture):
    item = load_fixture("gecko_new_pools.json")["data"][0]
    snap = provider().parse_pool(item, Chain.SOLANA)
    a = item["attributes"]
    assert snap.pool_address == a["address"]
    assert snap.base_token.address == item["relationships"]["base_token"]["data"]["id"].removeprefix("solana_")
    assert snap.base_token.symbol == a["name"].split(" / ")[0]
    assert snap.window("m5").buyers == a["transactions"]["m5"]["buyers"]
    assert snap.window("m15").volume_usd == Decimal(a["volume_usd"]["m15"])
    assert snap.price_usd == Decimal(a["base_token_price_usd"])
    assert snap.liquidity_usd is None if a["reserve_in_usd"] is None else snap.liquidity_usd > 0
    assert snap.dex_id == item["relationships"]["dex"]["data"]["id"]


def test_parse_pool_details(load_fixture):
    snap = provider().parse_pool(load_fixture("gecko_pool.json")["data"], Chain.SOLANA)
    assert snap.liquidity_usd is not None and snap.liquidity_usd > 0
    assert set(snap.windows) >= {"m5", "h1", "h24"}


def test_parse_rejects_non_pool():
    with pytest.raises(InvalidResponse):
        provider().parse_pool({"type": "token"}, Chain.SOLANA)


def test_parse_ohlcv_sorted_and_last_candle_open(load_fixture):
    data = load_fixture("gecko_ohlcv.json")
    rows = data["data"]["attributes"]["ohlcv_list"]
    newest = max(r[0] for r in rows)
    now = datetime.fromtimestamp(newest + 60, tz=timezone.utc)      # newest 5m candle still forming
    candles = GeckoTerminalProvider.parse_ohlcv(data, Timeframe.M5, now=now)
    assert len(candles) == len(rows)
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    assert not candles[-1].is_closed and all(c.is_closed for c in candles[:-1])
    assert all(c.high >= c.low for c in candles)


def test_parse_ohlcv_drops_malformed():
    data = {"data": {"attributes": {"ohlcv_list": [
        [1700000000, 1, 2, 0.5, 1.5, 10],
        [1700000300, 1, 0.4, 0.5, 1.5, 10],      # high < low
        [1700000600, None, 2, 0.5, 1.5, 10],     # missing open
        [1700000900, 1, 2, 0.5],                 # short row
    ]}}}
    candles = GeckoTerminalProvider.parse_ohlcv(data, Timeframe.M5)
    assert len(candles) == 1


def test_parse_ohlcv_missing_list():
    with pytest.raises(InvalidResponse):
        GeckoTerminalProvider.parse_ohlcv({"data": {}}, Timeframe.M1)


@respx.mock
async def test_get_ohlcv_request_params(load_fixture):
    pool = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
    route = respx.get(f"{BASE}/networks/solana/pools/{pool}/ohlcv/minute").mock(
        return_value=httpx.Response(200, json=load_fixture("gecko_ohlcv.json")))
    candles = await provider().get_ohlcv(Chain.SOLANA, pool, Timeframe.M15, limit=5000, before_ts=1700000000)
    q = route.calls[0].request.url.params
    assert q["aggregate"] == "15" and q["limit"] == "1000" and q["before_timestamp"] == "1700000000"
    assert candles


@respx.mock
async def test_get_trades(load_fixture):
    pool = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
    data = load_fixture("gecko_trades.json")
    respx.get(f"{BASE}/networks/solana/pools/{pool}/trades").mock(return_value=httpx.Response(200, json=data))
    trades = await provider().get_trades(Chain.SOLANA, pool)
    assert len(trades) == len(data["data"])
    assert all(t.kind in (TradeKind.BUY, TradeKind.SELL) and t.tx_hash for t in trades)


@respx.mock
async def test_new_pools_and_discover(load_fixture):
    respx.get(f"{BASE}/networks/solana/new_pools").mock(
        return_value=httpx.Response(200, json=load_fixture("gecko_new_pools.json")))
    leads = await provider().discover([Chain.SOLANA])
    assert leads and all(l.reason == "geckoterminal_new_pool" and l.pool_address for l in leads)


@respx.mock
async def test_trending_base_evm_addresses_lowercased(load_fixture):
    respx.get(f"{BASE}/networks/base/trending_pools").mock(
        return_value=httpx.Response(200, json=load_fixture("gecko_trending_pools.json")))
    pools = await provider().get_trending_pools(Chain.BASE)
    assert pools and all(p.pool_address == p.pool_address.lower() for p in pools)
