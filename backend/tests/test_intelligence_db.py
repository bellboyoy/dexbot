"""Market intelligence end to end against PostgreSQL: quality flags, metrics, momentum,
unique buyers, cross-provider de-duplication, pipeline separation and restart recovery."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.collector.worker import Collector, CollectorConfig
from app.db.models import (
    DataQualityEvent, DiscoveryLead, MarketSnapshot, MomentumEvent, Pool, PoolMetrics, Token, WatchedPool,
)
from app.discovery.pipelines import EstablishedPipeline, MomentumPipeline, NewLaunchPipeline, PipelineConfig
from app.discovery.screening import ScreeningConfig
from app.discovery.sources import GeckoPoolFeed, Lead, QuoteSeedFeed
from app.domain.chains import Chain
from app.domain.market import DataSource, WindowStats
from app.marketdata.scheduling import PriorityGate
from tests.factories import NOW, USDC_SOL, WSOL, sol_addr, snap

pytestmark = pytest.mark.postgres
VERIFIED = {Chain.SOLANA: {WSOL, USDC_SOL}}
SCREEN = ScreeningConfig(verified_quotes=VERIFIED)
PCFG = PipelineConfig(chains=(Chain.SOLANA,))

POOL, TOKEN = sol_addr(50), sol_addr(51)
OLD_POOL, OLD_TOKEN = sol_addr(52), sol_addr(53)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def active(s, clock):
    """Snapshot with full windows, observed at the current fake time."""
    w = dict(s.windows)
    w["m5"] = WindowStats(buys=6, sells=5, volume_usd=Decimal(1000), price_change_pct=Decimal("0.5"))
    w["h1"] = WindowStats(buys=60, sells=50, volume_usd=Decimal(12000), price_change_pct=Decimal(1))
    w["h24"] = WindowStats(buys=900, sells=800, volume_usd=Decimal(288000))
    return replace(s, windows=w, observed_at=clock.now)


class DS:
    def __init__(self, clock):
        self.clock = clock
        self.pools = {POOL: snap(pool=POOL, token=TOKEN),
                      OLD_POOL: snap(pool=OLD_POOL, token=OLD_TOKEN, age=timedelta(days=40), liquidity="900000",
                                     h24=(5000, 4000), vol24="2000000")}
        self.frozen_at = None

    def _snap(self, a):
        s = active(self.pools[a], self.clock)
        if self.frozen_at is not None:
            s = replace(s, observed_at=self.clock.now)          # same counters, new fetch time
        return s

    async def discover(self, chains, kinds=()):
        return []

    async def get_tokens(self, chain, tokens):
        return [self._snap(a) for a, s in self.pools.items() if s.base_token.address in tokens]

    async def get_pools(self, chain, addrs):
        return {a: self._snap(a) for a in addrs if a in self.pools}


class GT:
    def __init__(self, clock, ds):
        self.clock, self.ds, self.calls = clock, ds, 0

    async def get_new_pools(self, chain):
        self.calls += 1
        # Same pool DexScreener knows, reported by GeckoTerminal: must not duplicate rows.
        return [replace(active(snap(pool=POOL, token=TOKEN), self.clock), source=DataSource.GECKOTERMINAL)]

    async def get_trending_pools(self, chain):
        self.calls += 1
        return [replace(active(self.ds.pools[OLD_POOL], self.clock), source=DataSource.GECKOTERMINAL)]

    async def get_top_pools(self, chain):
        self.calls += 1
        return [replace(active(self.ds.pools[OLD_POOL], self.clock), source=DataSource.GECKOTERMINAL)]

    async def get_pools_multi(self, chain, addrs):
        self.calls += 1
        out = {}
        for a in addrs:
            s = replace(active(self.ds.pools[a], self.clock), source=DataSource.GECKOTERMINAL)
            w = dict(s.windows)
            w["h1"] = replace(w["h1"], buyers=41, sellers=17)
            out[a] = replace(s, windows=w, price_usd=s.price_usd * Decimal("1.5"))   # diverges from DexScreener
        return out

    async def get_ohlcv(self, *a, **k):
        return []


def build(sm, clock, ds=None, gt=None):
    ds = ds or DS(clock)
    gt = gt or GT(clock, ds)
    gate = PriorityGate()
    new = NewLaunchPipeline([GeckoPoolFeed(gt, gate, "new_pools", timedelta(minutes=5)),
                             GeckoPoolFeed(gt, gate, "trending", timedelta(minutes=15))], ds, sm, SCREEN, PCFG, clock=clock)
    est = EstablishedPipeline([QuoteSeedFeed(VERIFIED), GeckoPoolFeed(gt, gate, "top_pools", timedelta(hours=1))],
                              ds, sm, SCREEN, PCFG, clock=clock)
    col = Collector(ds, gt, gate, sm, [new, est], CollectorConfig(), clock=clock)
    return ds, gt, new, est, col


async def scalar(sm, q):
    async with sm() as s:
        return (await s.execute(q)).scalar_one()


async def test_pipelines_route_by_age_and_dedupe_across_providers(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    _, _, new, est, _ = build(sm, clock)
    n = await new.run_once()
    e = await est.run_once()
    assert n.added == 1 and n.other_profile == 1          # trending old pool left to the established pipeline
    assert e.added == 1
    async with sm() as s:
        rows = (await s.execute(select(WatchedPool.pipeline, WatchedPool.profile))).all()
        assert sorted(rows) == [("established", "established"), ("new", "new")]
        # POOL was reported by both GeckoTerminal and DexScreener: one pool row, one token row.
        assert (await s.execute(select(func.count()).select_from(Pool).where(Pool.address == POOL))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Token).where(Token.address == TOKEN))).scalar_one() == 1
        pipelines = set((await s.execute(select(DiscoveryLead.pipeline))).scalars())
    assert pipelines == {"new", "established"}


async def test_snapshot_metrics_quality_and_momentum(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds, _, new, est, col = build(sm, clock)
    await new.run_once(); await est.run_once()
    await col.snapshot_once()
    clock.now += timedelta(minutes=5)
    # Volume and buying surge on the established pool.
    s = ds.pools[OLD_POOL]
    ds.pools[OLD_POOL] = replace(s, liquidity_usd=Decimal(1_000_000))
    orig = ds._snap

    def surging(a):
        x = orig(a)
        if a != OLD_POOL:
            return x
        w = dict(x.windows)
        w["m5"] = WindowStats(buys=80, sells=20, volume_usd=Decimal(60000), price_change_pct=Decimal(4))
        return replace(x, windows=w)
    ds._snap = surging
    await col.snapshot_once()
    async with sm() as s:
        m = (await s.execute(select(PoolMetrics).join(Pool, Pool.id == PoolMetrics.pool_id)
                             .where(Pool.address == OLD_POOL).order_by(PoolMetrics.observed_at.desc()))).scalars().first()
        assert m.reliable and m.volume_accel_m5 == Decimal(60) and m.buy_ratio_m5 == Decimal("0.8")
        assert m.liquidity_change_5m_pct is not None and m.liquidity_change_5m_pct > 0
        ev = (await s.execute(select(MomentumEvent))).scalars().all()
    assert len(ev) == 1 and {"volume_spike", "buy_pressure"} <= set(ev[0].triggers)


async def test_frozen_provider_flagged_and_metrics_unreliable(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds, _, new, _, col = build(sm, clock)
    await new.run_once()
    await col.snapshot_once()
    clock.now += timedelta(minutes=4)                 # counters identical 4 minutes later -> frozen
    await col.snapshot_once()
    async with sm() as s:
        flags = (await s.execute(select(DataQualityEvent.check))).scalars().all()
        last = (await s.execute(select(PoolMetrics).order_by(PoolMetrics.observed_at.desc()))).scalars().first()
        snap_flags = (await s.execute(select(MarketSnapshot.quality_flags)
                                      .order_by(MarketSnapshot.observed_at.desc()))).scalars().first()
    assert "frozen" in flags and not last.reliable and "frozen" in snap_flags


async def test_unique_buyers_and_divergence(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    _, gt, new, _, col = build(sm, clock)
    await new.run_once()
    await col.snapshot_once()
    assert await col.buyers_once() == 1
    async with sm() as s:
        checks = set((await s.execute(select(DataQualityEvent.check))).scalars())
        sources = set((await s.execute(select(MarketSnapshot.source))).scalars())
    assert "source_divergence_price" in checks and sources == {"dexscreener", "geckoterminal"}
    # The diverging GeckoTerminal observation is warn-level: buyers still usable.
    clock.now += timedelta(seconds=31)
    await col.snapshot_once()
    async with sm() as s:
        m = (await s.execute(select(PoolMetrics).order_by(PoolMetrics.observed_at.desc()))).scalars().first()
    assert m.buyers_h1 == 41 and m.sellers_h1 == 17


async def test_restart_recovery(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds, gt, new, est, col = build(sm, clock)
    await col.restore()
    await new.run_once(); await est.run_once()
    await col.snapshot_once()
    calls_before = gt.calls
    pools_before = await scalar(sm, select(func.count()).select_from(Pool))

    # "Crash" and restart 5 minutes later: brand-new objects, same database.
    clock.now += timedelta(minutes=5)
    ds2, gt2, new2, est2, col2 = build(sm, clock, ds=ds)
    await col2.restore()
    await est2.run_once()                       # top_pools (hourly) not due again: no GeckoTerminal call
    assert gt2.calls == 0
    await col2.snapshot_once()
    async with sm() as s:
        m = (await s.execute(select(PoolMetrics).order_by(PoolMetrics.observed_at.desc()))).scalars().first()
    assert m.liquidity_change_5m_pct is not None        # history primed from the database
    assert await scalar(sm, select(func.count()).select_from(Pool)) == pools_before
    assert await scalar(sm, select(func.count()).select_from(WatchedPool)) == 2


async def test_momentum_pipeline_requires_momentum(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds = DS(clock)
    gt = GT(clock, ds)
    gate = PriorityGate()
    mom = MomentumPipeline([GeckoPoolFeed(gt, gate, "trending", timedelta(minutes=15))], ds, sm, SCREEN, PCFG,
                           clock=clock)
    st = await mom.run_once()
    assert st.added == 0 and st.rejections["no_momentum"] == 1
    orig = ds._snap

    def hot(a):
        x = orig(a)
        w = dict(x.windows)
        w["m5"] = WindowStats(buys=80, sells=20, volume_usd=Decimal(60000))
        return replace(x, windows=w)
    ds._snap = hot
    clock.now += timedelta(minutes=16)
    st = await mom.run_once()
    assert st.added == 1
    async with sm() as s:
        assert (await s.execute(select(WatchedPool.pipeline))).scalar_one() == "momentum"
