"""Historical market-data collector.

Runs independent loops, each isolated so one failing does not stop the others:

  discovery_*  - one loop per discovery pipeline (new, established, momentum)
  snapshot     - DexScreener snapshot of every watched pool on a status-based interval
                 (30 pools per request); each snapshot is quality-checked, turned into
                 metrics (volume acceleration, buy/sell pressure, liquidity change) and
                 checked for unusual momentum
  buyers       - GeckoTerminal multi-pool snapshots: unique buyers/sellers and a
                 second-source cross-check (price/liquidity divergence)
  ohlcv        - initial 1m + 15m backfill, then 1m refreshes, behind the priority gate
                 (discovery > buyers > backfill)

Watched pools move active -> inactive -> dead -> retired. Dead pools keep being recorded
for `retire_after`, so failures are in the dataset (no survivorship bias). A provider
outage never counts as a pool going missing.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repository as repo
from app.db.models import WatchedPool
from app.discovery.pipelines import DiscoveryPipeline
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot, Timeframe
from app.intelligence.metrics import MetricsEngine
from app.intelligence.momentum import MomentumDetector
from app.quality.checks import QualityConfig, check_divergence, check_snapshot
from app.logging import get_logger
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import ProviderError
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.parsing import utcnow
from app.marketdata.scheduling import Priority, PriorityGate

log = get_logger(__name__)


@dataclass(frozen=True)
class CollectorConfig:
    snapshot_intervals: dict[str, timedelta] = field(default_factory=lambda: {
        "active": timedelta(seconds=30), "inactive": timedelta(minutes=10), "dead": timedelta(minutes=30)})
    ohlcv_refresh: dict[str, timedelta] = field(default_factory=lambda: {
        "active": timedelta(hours=6), "inactive": timedelta(hours=12), "dead": timedelta(hours=24)})
    ohlcv_error_backoff: timedelta = timedelta(minutes=15)
    liquidity_floor_usd: Decimal = Decimal(5_000)
    dead_liquidity_usd: Decimal = Decimal(1_000)
    miss_limit: int = 10
    retire_after: timedelta = timedelta(hours=72)
    discovery_every_s: float = 60
    snapshot_tick_s: float = 10
    ohlcv_tick_s: float = 5
    ohlcv_batch: int = 3
    buyers_every_s: float = 90          # one GeckoTerminal multi-pool call (30 pools) per tick
    buyers_batch: int = 30
    restore_window: timedelta = timedelta(hours=2)
    # Initial backfill fetches these; later refreshes fetch only the first. Other timeframes
    # (5m, 1h, 4h) are resampled from stored candles on read.
    backfill_timeframes: tuple[Timeframe, ...] = (Timeframe.M1, Timeframe.M15)


def next_status(current: str, status_since: datetime, snap: PoolSnapshot | None, misses: int,
                now: datetime, cfg: CollectorConfig) -> str:
    """Lifecycle transition for a watched pool after one snapshot attempt."""
    if current == "retired":
        return "retired"
    if snap is None:
        status = "dead" if misses >= cfg.miss_limit else current
    else:
        liq = snap.liquidity_usd
        w = snap.window("h1")
        h1 = None if w.buys is None or w.sells is None else w.buys + w.sells
        if liq is not None and liq < cfg.dead_liquidity_usd:
            status = "dead"
        elif liq is None or liq < cfg.liquidity_floor_usd or h1 == 0:
            status = "inactive"
        else:
            status = "active"
    if status in ("inactive", "dead") and status == current and now - status_since >= cfg.retire_after:
        return "retired"
    return status


@dataclass
class CollectorStats:
    snapshots: int = 0
    misses: int = 0
    status_changes: int = 0
    candles: int = 0
    ohlcv_jobs: int = 0
    buyer_updates: int = 0
    quality_flags: int = 0
    momentum_events: int = 0
    errors: int = 0


class Collector:
    def __init__(self, ds: DexScreenerProvider, gt: GeckoTerminalProvider, gate: PriorityGate,
                 sessionmaker: async_sessionmaker, pipelines: list[DiscoveryPipeline] | None = None,
                 cfg: CollectorConfig | None = None, clock: Callable[[], datetime] = utcnow,
                 quality_cfg: QualityConfig | None = None):
        self.ds, self.gt, self.gate, self.sm = ds, gt, gate, sessionmaker
        self.pipelines = pipelines or []
        self.cfg = cfg or CollectorConfig()
        self.quality_cfg = quality_cfg or QualityConfig()
        self.clock = clock
        self.stats = CollectorStats()
        self.metrics = MetricsEngine()
        self.momentum = MomentumDetector()
        self._restored = False
        self._buyers_chain_rr = 0

    async def restore(self) -> None:
        """Rebuild in-memory history after a restart from what is already in the database,
        so liquidity-change metrics and momentum cooldowns continue without a gap."""
        since = self.clock() - self.cfg.restore_window
        async with self.sm() as s:
            for chain, addr, at, liq in await repo.recent_liquidity(s, since):
                self.metrics.prime_liquidity((chain, addr), at, liq)
            for chain, addr, at in await repo.recent_momentum(s, self.clock() - self.momentum.cfg.cooldown):
                self.momentum.prime((chain, addr), at)
        for p in self.pipelines:
            await p.restore()
        self._restored = True
        log.info("collector_restored", since=since.isoformat())

    async def snapshot_once(self) -> int:
        now = self.clock()
        async with self.sm() as session:
            due = await repo.due_for_snapshot(session, now, self.cfg.snapshot_intervals)
            by_chain: dict[Chain, list[tuple[WatchedPool, str]]] = {}
            for wp, addr in due:
                by_chain.setdefault(wp.chain, []).append((wp, addr))
            recorded = 0
            for chain, items in by_chain.items():
                try:
                    found = await self.ds.get_pools(chain, [a for _, a in items])
                except ProviderError as exc:
                    # Outage: record nothing, count no misses, try again next tick.
                    self.stats.errors += 1
                    log.warning("snapshot_chain_failed", chain=chain, pools=len(items), error=str(exc))
                    await repo.log_event(session, "warning", "collector", "snapshot_chain_failed", chain,
                                         error=str(exc), pools=len(items))
                    await repo.record_quality(session, chain=chain, pool_id=None, source="dexscreener",
                                              observed_at=now, flags=["provider_unavailable"],
                                              details={"provider_unavailable": {"error": str(exc), "pools": len(items)}})
                    continue
                for wp, addr in items:
                    snap = found.get(addr)
                    misses = 0 if snap else wp.consecutive_misses + 1
                    if snap is not None:
                        await self._record(session, wp, snap, now)
                        recorded += 1
                    else:
                        self.stats.misses += 1
                    new = next_status(wp.status, wp.status_since, snap, misses, now, self.cfg)
                    values = {"last_snapshot_at": now, "consecutive_misses": misses}
                    if new != wp.status:
                        values.update(status=new, status_since=now)
                        self.stats.status_changes += 1
                        log.info("watch_status_changed", chain=chain, pool=addr, old=wp.status, new=new)
                    await repo.update_watch(session, wp.id, **values)
            await session.commit()
        self.stats.snapshots += recorded
        return recorded

    async def _record(self, session, wp: WatchedPool, snap: PoolSnapshot, now: datetime) -> None:
        key = (snap.chain, snap.pool_address)
        q = check_snapshot(snap, now, self.metrics.last(key), self.quality_cfg)
        await repo.insert_snapshot(session, wp.pool_id, snap, quality_flags=q.flags)
        if q.flags:
            self.stats.quality_flags += len(q.flags)
            await repo.record_quality(session, chain=snap.chain, pool_id=wp.pool_id, source=snap.source.value,
                                      observed_at=snap.observed_at, flags=q.flags, details=q.details)
        m = self.metrics.compute(snap, q)
        metrics_id = await repo.insert_metrics(session, wp.pool_id, m.row())
        trig = self.momentum.evaluate(m, wp.profile)
        if trig:
            self.stats.momentum_events += 1
            await repo.record_momentum(session, chain=snap.chain, pool_id=wp.pool_id, profile=wp.profile,
                                       detected_at=m.observed_at, metrics_id=metrics_id, triggers=trig,
                                       details={k: v for k, v in m.row().items() if k not in ("chain", "quality_flags")})
            log.info("momentum_event", chain=snap.chain, pool=snap.pool_address, profile=wp.profile, triggers=trig)

    async def buyers_once(self) -> int:
        """Unique buyer/seller counts (and a second-source cross-check) from GeckoTerminal's
        multi-pool endpoint, one chain per tick in rotation, oldest-checked pools first."""
        chains = sorted({p for p in Chain}, key=lambda c: c.value)
        async with self.sm() as session:
            active_chains = [c for c in chains if await repo.buyers_due(session, c, 1)]
        if not active_chains:
            return 0
        chain = active_chains[self._buyers_chain_rr % len(active_chains)]
        self._buyers_chain_rr += 1
        async with self.sm() as session:
            items = await repo.buyers_due(session, chain, self.cfg.buyers_batch)
            try:
                async with self.gate.slot(Priority.BUYERS):
                    found = await self.gt.get_pools_multi(chain, [a for _, a in items])
            except ProviderError as exc:
                self.stats.errors += 1
                log.warning("buyers_failed", chain=chain, error=str(exc))
                await repo.record_quality(session, chain=chain, pool_id=None, source="geckoterminal",
                                          observed_at=self.clock(), flags=["provider_unavailable"],
                                          details={"provider_unavailable": {"error": str(exc)}})
                await session.commit()
                return 0
            now = self.clock()
            for wp, addr in items:
                snap = found.get(addr)
                await repo.update_watch(session, wp.id, buyers_checked_at=now)
                if snap is None:
                    continue
                q = check_snapshot(snap, now, None, self.quality_cfg)
                primary = self.metrics.last((chain, addr))
                if primary is not None and abs(primary.observed_at - snap.observed_at) <= timedelta(minutes=2):
                    div = check_divergence(primary, snap, self.quality_cfg)
                    q.flags.extend(f for f in div.flags if f not in q.flags)
                    q.details.update(div.details)
                await repo.insert_snapshot(session, wp.pool_id, snap, quality_flags=q.flags)
                if q.flags:
                    self.stats.quality_flags += len(q.flags)
                    await repo.record_quality(session, chain=chain, pool_id=wp.pool_id, source=snap.source.value,
                                              observed_at=snap.observed_at, flags=q.flags, details=q.details)
                if q.reliable:
                    self.metrics.observe_buyers(snap)
                    self.stats.buyer_updates += 1
            await session.commit()
        return len(found)

    async def ohlcv_once(self) -> int:
        now = self.clock()
        async with self.sm() as session:
            jobs = await repo.due_for_ohlcv(session, now, self.cfg.ohlcv_batch)
        inserted = 0
        for wp, addr in jobs:
            frames = list(self.cfg.backfill_timeframes[:1] if wp.ohlcv_backfilled else self.cfg.backfill_timeframes)
            try:
                fetched = []
                for tf in frames:
                    async with self.gate.slot(Priority.BACKFILL):
                        fetched.append(await self.gt.get_ohlcv(wp.chain, addr, tf, limit=1000))
            except ProviderError as exc:
                self.stats.errors += 1
                log.warning("ohlcv_failed", chain=wp.chain, pool=addr, error=str(exc))
                async with self.sm() as session:
                    await repo.update_watch(session, wp.id, next_ohlcv_at=self.clock() + self.cfg.ohlcv_error_backoff)
                    await session.commit()
                continue
            async with self.sm() as session:
                for candles in fetched:
                    inserted += await repo.upsert_candles(session, wp.pool_id, wp.chain, "geckoterminal", candles)
                refresh = self.cfg.ohlcv_refresh.get(wp.status, self.cfg.ohlcv_refresh["dead"])
                await repo.update_watch(session, wp.id, ohlcv_backfilled=True, next_ohlcv_at=self.clock() + refresh)
                await session.commit()
            self.stats.ohlcv_jobs += 1
        self.stats.candles += inserted
        return inserted

    async def _loop(self, name: str, fn, every_s: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await fn()
            except Exception as exc:          # isolate: one loop failing must not kill the others
                self.stats.errors += 1
                log.exception("collector_loop_error", loop=name, error=str(exc))
                try:
                    async with self.sm() as session:
                        await repo.log_event(session, "error", "collector", f"{name}_loop_error", error=str(exc))
                        await session.commit()
                except Exception:
                    log.exception("collector_log_event_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=every_s)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event, grace_s: float = 10.0) -> None:
        """Run until `stop` is set, then give in-flight work `grace_s` to finish and cancel the
        rest. A loop may be parked for minutes behind a rate-limit penalty, so waiting for it
        to notice `stop` is not enough. Cancelled DB work rolls back; nothing is half-written."""
        if not self._restored:
            await self.restore()
        loops = [self._loop("snapshot", self.snapshot_once, self.cfg.snapshot_tick_s, stop),
                 self._loop("ohlcv", self.ohlcv_once, self.cfg.ohlcv_tick_s, stop),
                 self._loop("buyers", self.buyers_once, self.cfg.buyers_every_s, stop)]
        for p in self.pipelines:
            loops.append(self._loop(f"discovery_{p.name}", p.run_once, self.cfg.discovery_every_s, stop))
        tasks = [asyncio.create_task(c) for c in loops]
        await stop.wait()
        _, pending = await asyncio.wait(tasks, timeout=grace_s)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
