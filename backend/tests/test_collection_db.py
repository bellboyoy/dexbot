"""Discovery + collector against a real PostgreSQL with fake providers."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.collector.worker import Collector, CollectorConfig
from app.db.models import (
    CandleRow, DataQualityEvent, DiscoveryLead, MarketSnapshot, Pool, PoolMetrics, SystemLog, Token, WatchedPool,
)
from app.discovery.pipelines import NewLaunchPipeline, PipelineConfig
from app.discovery.screening import ScreeningConfig
from app.discovery.sources import DexScreenerFeed, GeckoPoolFeed, QuoteSeedFeed
from app.domain.chains import Chain
from app.domain.market import Candle, DataSource, DiscoveredToken, Timeframe
from app.marketdata.errors import ProviderUnavailable
from app.marketdata.scheduling import PriorityGate
from tests.factories import NOW, USDC_SOL, WSOL, sol_addr, snap

pytestmark = pytest.mark.postgres

GOOD_TOKEN, BAD_TOKEN, NEWPOOL_TOKEN, GHOST_TOKEN = sol_addr(10), sol_addr(11), sol_addr(12), sol_addr(13)
GOOD_POOL, BAD_POOL, NEW_POOL = sol_addr(20), sol_addr(21), sol_addr(22)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeDS:
    def __init__(self):
        self.fail = False
        self.pools = {
            GOOD_POOL: snap(pool=GOOD_POOL, token=GOOD_TOKEN),
            BAD_POOL: snap(pool=BAD_POOL, token=BAD_TOKEN, liquidity="800"),
            sol_addr(23): snap(pool=sol_addr(23), token=GOOD_TOKEN, liquidity="999999",
                               quote=sol_addr(98), quote_symbol="MEME"),   # deeper but not exitable
        }

    async def discover(self, chains, kinds=("profiles", "boosts")):
        if "profiles" not in kinds:
            return []
        return [DiscoveredToken(DataSource.DEXSCREENER, Chain.SOLANA, t, None, NOW, "dexscreener_profile")
                for t in (GOOD_TOKEN, BAD_TOKEN, GHOST_TOKEN)]

    async def get_tokens(self, chain, tokens):
        if self.fail:
            raise ProviderUnavailable("dexscreener", "down")
        return [s for s in self.pools.values() if s.base_token.address in tokens]

    async def get_pools(self, chain, addrs):
        if self.fail:
            raise ProviderUnavailable("dexscreener", "down")
        return {a: self.pools[a] for a in addrs if a in self.pools}


class FakeGT:
    def __init__(self):
        self.ohlcv_calls = 0

    async def get_new_pools(self, chain):
        return [snap(pool=NEW_POOL, token=NEWPOOL_TOKEN, source=DataSource.GECKOTERMINAL)]

    async def get_trending_pools(self, chain):
        return []

    async def get_top_pools(self, chain):
        return []

    async def get_pools_multi(self, chain, addrs):
        return {}

    async def get_ohlcv(self, chain, pool, tf, limit=100):
        self.ohlcv_calls += 1
        base = NOW - timedelta(minutes=10 * tf.seconds // 60)
        return [Candle(base + timedelta(seconds=i * tf.seconds), tf, *(Decimal(1),) * 4, Decimal(10),
                       is_closed=i < 9) for i in range(10)]


VERIFIED = {Chain.SOLANA: {WSOL, USDC_SOL}}


def build(sm, clock, ds=None, gt=None, max_watchlist=300):
    ds, gt = ds or FakeDS(), gt or FakeGT()
    gate = PriorityGate()
    sources = [GeckoPoolFeed(gt, gate, "new_pools", timedelta(minutes=5)), DexScreenerFeed(ds, "profiles"),
               QuoteSeedFeed(VERIFIED)]
    disc = NewLaunchPipeline(sources, ds, sm, ScreeningConfig(verified_quotes=VERIFIED),
                             PipelineConfig(chains=(Chain.SOLANA,), max_watchlist=max_watchlist), clock=clock)
    cfg = CollectorConfig(backfill_timeframes=(Timeframe.M1, Timeframe.M5))
    return ds, gt, disc, Collector(ds, gt, gate, sm, [disc], cfg, clock=clock)


async def count(sm, model, *where):
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_discovery_records_everything_and_watches_passing(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    _, _, disc, _ = build(sm, clock)
    stats = await disc.run_once()
    # good token (best exitable pool), gecko new pool, both seeds unresolved by fake; bad rejected; ghost unresolved
    async with sm() as s:
        watched = {(w.base_token_address, w.discovered_via) for w in (await s.execute(select(WatchedPool))).scalars()}
        leads = (await s.execute(select(DiscoveryLead))).scalars().all()
    assert watched == {(GOOD_TOKEN, "dexscreener_profile"), (NEWPOOL_TOKEN, "geckoterminal_new_pools")}
    by_token = {l.token_address: l for l in leads}
    assert by_token[GOOD_TOKEN].pool_address == GOOD_POOL                       # exitable pool chosen over deeper MEME pool
    assert by_token[BAD_TOKEN].passed is False and "low_liquidity" in by_token[BAD_TOKEN].rejections["reasons"]
    assert by_token[GHOST_TOKEN].passed is None                                  # unresolved, still recorded
    assert stats.added == 2 and await count(sm, MarketSnapshot) == 2

    # Immediately again: no source is due, so no provider is called at all.
    assert (await disc.run_once()).leads == 0
    # A minute later only the DexScreener feed is due (GeckoTerminal new pools: 5 min); its
    # tokens were screened within the re-screen window, so nothing is re-evaluated.
    clock.now += timedelta(seconds=61)
    again = await disc.run_once()
    assert again.leads == 3 and again.skipped_recent == 3 and again.added == 0


async def test_watchlist_cap(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    _, _, disc, _ = build(sm, clock, max_watchlist=1)
    stats = await disc.run_once()
    assert stats.added == 1 and stats.watchlist_full == 1


async def test_discovery_survives_resolution_outage(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds = FakeDS(); ds.fail = True
    _, _, disc, _ = build(sm, clock, ds=ds)
    stats = await disc.run_once()
    assert stats.source_errors and stats.added == 1        # gecko new pool still added from its own snapshot
    assert all("resolve" in e for e in stats.source_errors)


async def test_snapshots_lifecycle_and_outage(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    ds, _, disc, col = build(sm, clock)
    await disc.run_once()
    before = await count(sm, MarketSnapshot)

    # Not due yet (just snapshotted at discovery? no: last_snapshot_at is None) -> first tick records both.
    assert await col.snapshot_once() == 1        # NEW_POOL is unknown to DexScreener -> miss
    assert await count(sm, MarketSnapshot) == before + 1
    assert await col.snapshot_once() == 0        # nothing due within 30s

    # Outage: no snapshots, no misses counted.
    clock.now += timedelta(seconds=31)
    ds.fail = True
    assert await col.snapshot_once() == 0
    async with sm() as s:
        misses = {w.base_token_address: w.consecutive_misses for w in (await s.execute(select(WatchedPool))).scalars()}
    assert misses[NEWPOOL_TOKEN] == 1 and misses[GOOD_TOKEN] == 0
    assert await count(sm, SystemLog, SystemLog.event == "snapshot_chain_failed") == 1

    # Recovery, then the good pool's liquidity collapses -> dead; the missing pool dies after the miss limit.
    ds.fail = False
    ds.pools[GOOD_POOL] = replace(ds.pools[GOOD_POOL], liquidity_usd=Decimal(100))
    for _ in range(CollectorConfig().miss_limit):
        clock.now += timedelta(minutes=31)
        await col.snapshot_once()
    async with sm() as s:
        status = {w.base_token_address: w.status for w in (await s.execute(select(WatchedPool))).scalars()}
    assert status == {GOOD_TOKEN: "dead", NEWPOOL_TOKEN: "dead"}
    # Dead pools are still recorded (survivorship): good pool kept getting snapshots.
    assert await count(sm, MarketSnapshot) > before + 2

    clock.now += CollectorConfig().retire_after
    await col.snapshot_once()
    assert await count(sm, WatchedPool, WatchedPool.status == "retired") == 2


async def test_ohlcv_backfill_idempotent(pg_sessionmaker):
    sm, clock = pg_sessionmaker, Clock()
    _, gt, disc, col = build(sm, clock)
    await disc.run_once()
    inserted = await col.ohlcv_once()
    assert inserted == 2 * 2 * 9                 # 2 pools x (1m + 5m) x 9 closed candles (forming one skipped)
    assert gt.ohlcv_calls == 4
    assert await col.ohlcv_once() == 0           # nothing due
    clock.now += timedelta(hours=7)
    assert await col.ohlcv_once() == 0           # refresh: 1m only, same candles -> no duplicates
    assert gt.ohlcv_calls == 6
    assert await count(sm, CandleRow) == 36


async def test_collector_status_endpoint(pg_sessionmaker):
    import httpx

    from app.main import app
    sm, clock = pg_sessionmaker, Clock()
    _, _, disc, col = build(sm, clock)
    await disc.run_once()
    await col.ohlcv_once()
    app.state.sessionmaker = sm
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/collector/status")).json()
        hist = (await c.get(f"/history/solana/pools/{GOOD_POOL}/candles", params={"timeframe": "15m"})).json()
        quality = (await c.get("/quality")).json()
    sol = body["chains"]["solana"]
    assert sum(w["pools"] for w in sol["watchlist"]) == 2
    assert sum(sol["candles"].values()) == 36 and sol["snapshots"]["dexscreener"]["total"] == 1
    assert {(l["outcome"], l["count"]) for l in sol["leads"]} == {("passed", 2), ("rejected", 1), ("unresolved", 3)}
    assert hist["derived_from_1m"] is True and hist["timeframe"] == "15m"
    assert isinstance(quality, list)


async def test_shutdown_cancels_loops_stuck_behind_rate_limits(pg_sessionmaker):
    import asyncio
    sm, clock = pg_sessionmaker, Clock()

    class StuckGT(FakeGT):
        async def get_ohlcv(self, *a, **k):
            await asyncio.sleep(3600)               # e.g. parked behind a long 429 penalty

    _, _, disc, col = build(sm, clock, gt=StuckGT())
    await disc.run_once()
    stop = asyncio.Event()
    runner = asyncio.create_task(col.run(stop, grace_s=0.2))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(runner, 2)               # returns promptly instead of hanging
