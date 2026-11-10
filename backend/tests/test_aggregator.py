from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.chains import Chain
from app.domain.market import Candle, DataSource, DiscoveredToken, PoolSnapshot, Timeframe, TokenRef, WindowStats
from app.marketdata.aggregator import MarketDataService, merge_snapshots, price_divergence
from app.marketdata.base import Capability
from app.marketdata.errors import ProviderUnavailable
from app.marketdata.http import ProviderStatus

POOL = "So11111111111111111111111111111111111111112"
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def snap(source, price="1", **kw):
    base = dict(source=source, chain=Chain.SOLANA, pool_address=POOL, dex_id=None,
                base_token=TokenRef(Chain.SOLANA, POOL), quote_token=None, observed_at=NOW,
                price_usd=Decimal(price), price_native=None, liquidity_usd=None, fdv_usd=None,
                market_cap_usd=None, pool_created_at=None, windows={})
    base.update(kw)
    return PoolSnapshot(**base)


class Fake:
    def __init__(self, name, caps, pool=None, fail=False, leads=(), candles=()):
        self.name, self.capabilities = name, frozenset(caps)
        self._pool, self._fail, self._leads, self._candles = pool, fail, list(leads), list(candles)
        self.calls = 0

    async def get_pool(self, chain, addr):
        self.calls += 1
        if self._fail:
            raise ProviderUnavailable(self.name, "down")
        return self._pool

    async def discover(self, chains):
        if self._fail:
            raise ProviderUnavailable(self.name, "down")
        return self._leads

    async def get_ohlcv(self, *a):
        return self._candles

    def status(self):
        return ProviderStatus(self.name, "closed", None, None, 0, 0, 0)

    async def aclose(self):
        pass


def test_merge_fills_only_unknowns():
    a = snap(DataSource.DEXSCREENER, "1.00", liquidity_usd=Decimal(100),
             windows={"m5": WindowStats(buys=10, sells=5)})
    b = snap(DataSource.GECKOTERMINAL, "1.02", liquidity_usd=Decimal(999), fdv_usd=Decimal(5000),
             windows={"m5": WindowStats(buys=11, buyers=7), "m15": WindowStats(buys=20)})
    m = merge_snapshots(a, b)
    assert m.price_usd == Decimal("1.00") and m.liquidity_usd == 100 and m.fdv_usd == 5000
    assert m.window("m5") == WindowStats(buys=10, sells=5, buyers=7)
    assert m.window("m15").buys == 20 and m.source is DataSource.DEXSCREENER


def test_merge_rejects_different_pools():
    with pytest.raises(ValueError):
        merge_snapshots(snap(DataSource.DEXSCREENER),
                        replace(snap(DataSource.GECKOTERMINAL), pool_address="other"))


def test_price_divergence():
    assert price_divergence(snap(DataSource.DEXSCREENER, "1"), snap(DataSource.GECKOTERMINAL, "1.1")) == Decimal("0.1")
    assert price_divergence(snap(DataSource.DEXSCREENER, "1"), snap(DataSource.GECKOTERMINAL, "0")) is None


async def test_failover_to_second_provider():
    a = Fake("dexscreener", {Capability.POOL_SNAPSHOT}, fail=True)
    b = Fake("geckoterminal", {Capability.POOL_SNAPSHOT}, pool=snap(DataSource.GECKOTERMINAL))
    got = await MarketDataService([a, b]).get_pool(Chain.SOLANA, POOL)
    assert got.source is DataSource.GECKOTERMINAL and a.calls == 1


async def test_all_fail_raises():
    a = Fake("dexscreener", {Capability.POOL_SNAPSHOT}, fail=True)
    with pytest.raises(ProviderUnavailable):
        await MarketDataService([a]).get_pool(Chain.SOLANA, POOL)


async def test_no_capable_provider_raises():
    a = Fake("dexscreener", {Capability.POOL_SNAPSHOT})
    with pytest.raises(ProviderUnavailable, match="ohlcv"):
        await MarketDataService([a]).get_ohlcv(Chain.SOLANA, POOL, Timeframe.M5)


async def test_enrich_tolerates_failure():
    a = Fake("dexscreener", {Capability.POOL_SNAPSHOT}, pool=snap(DataSource.DEXSCREENER))
    b = Fake("geckoterminal", {Capability.POOL_SNAPSHOT}, fail=True)
    got = await MarketDataService([a, b]).get_pool(Chain.SOLANA, POOL, enrich=True)
    assert got.source is DataSource.DEXSCREENER and b.calls == 1


async def test_discover_dedupes_and_survives_failures():
    lead = DiscoveredToken(DataSource.DEXSCREENER, Chain.SOLANA, POOL, None, NOW, "x")
    a = Fake("dexscreener", set(), leads=[lead, lead])
    b = Fake("geckoterminal", set(), fail=True)
    assert await MarketDataService([a, b]).discover([Chain.SOLANA]) == [lead]


async def test_ohlcv_requests_one_extra_closed_bar():
    class Recorder:
        name = "geckoterminal"
        capabilities = frozenset({Capability.OHLCV})

        def __init__(self):
            self.limit = None

        async def get_ohlcv(self, chain, pool, timeframe, limit=100, before_ts=None):
            self.limit = limit
            return []

        def status(self):
            return ProviderStatus(self.name, "closed", None, None, 0, 0, 0)

        async def aclose(self):
            pass

    recorder = Recorder()
    await MarketDataService([recorder]).get_ohlcv(Chain.SOLANA, POOL, Timeframe.M5, limit=10)
    assert recorder.limit == 11


async def test_ohlcv_drops_forming_candle():
    closed = Candle(NOW, Timeframe.M5, *(Decimal(1),) * 5, is_closed=True)
    forming = replace(closed, is_closed=False)
    svc = MarketDataService([Fake("geckoterminal", {Capability.OHLCV}, candles=[closed, forming])])
    assert await svc.get_ohlcv(Chain.SOLANA, POOL, Timeframe.M5) == [closed]
    assert len(await svc.get_ohlcv(Chain.SOLANA, POOL, Timeframe.M5, closed_only=False)) == 2
