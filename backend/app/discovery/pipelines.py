"""Discovery pipelines.

  NewLaunchPipeline     pools up to 72h old: GeckoTerminal new pools, DexScreener profiles
                        and boosts, trending; screened with the new-token profile
  EstablishedPipeline   older pools: chain seeds, GeckoTerminal top pools and trending;
                        screened with the established-token profile
  MomentumPipeline      (app/discovery/momentum_pipeline.py) unusual momentum among
                        established tokens, from trending feeds and from collected metrics

Each pipeline has its own sources, schedule, screening profile and stats, and records
every lead it evaluates (pass, fail or unresolved) with `pipeline` set. Leads whose pool
belongs to the other age profile are left to that pipeline. Source schedules and recent
screening times are persisted, so a restart neither re-hits rate-limited feeds nor
re-screens everything.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repository as repo
from app.discovery.screening import Profile, ScreeningConfig, classify, screen
from app.discovery.sources import DiscoverySource, Lead
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.logging import get_logger
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import ProviderError
from app.marketdata.parsing import iso, utcnow

log = get_logger(__name__)


@dataclass
class PipelineStats:
    leads: int = 0
    skipped_recent: int = 0
    other_profile: int = 0
    unresolved: int = 0
    screened: int = 0
    passed: int = 0
    added: int = 0
    watchlist_full: int = 0
    source_errors: list[str] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class PipelineConfig:
    chains: tuple[Chain, ...]
    max_watchlist: int = 300
    rescreen_after: timedelta = timedelta(minutes=10)
    # Rate-limited fetches per cycle. The rest stay due and run next cycle, so cheap
    # sources are never held up behind a queue of scarce ones.
    max_gated_per_cycle: int = 1


def best_pool(snaps: list[PoolSnapshot], token: str, cfg: ScreeningConfig) -> PoolSnapshot | None:
    """Deepest pool where `token` is the base and the quote is exitable; otherwise the
    deepest pool where it is the base (screening will then reject the quote)."""
    own = [s for s in snaps if s.base_token.address == token]
    if not own:
        return None
    exitable = [s for s in own if cfg.quote_allowed(s.chain, s.quote_token.address if s.quote_token else None)]
    return max(exitable or own, key=lambda s: s.liquidity_usd if s.liquidity_usd is not None else -1)


class DiscoveryPipeline:
    profile: Profile
    name: str

    def __init__(self, sources: list[DiscoverySource], resolver: DexScreenerProvider,
                 sessionmaker: async_sessionmaker, screening: ScreeningConfig, cfg: PipelineConfig,
                 clock: Callable[[], datetime] = utcnow):
        self.sources, self.resolver, self.sm = sources, resolver, sessionmaker
        self.screening, self.cfg, self.clock = screening, cfg, clock
        self._screened_at: dict[tuple[Chain, str], datetime] = {}
        self._last_run: dict[str, datetime] = {}
        self._restored = False

    # --- persistence of schedules (restart recovery) -----------------------------
    @property
    def _state_key(self) -> str:
        return f"pipeline:{self.name}:last_run"

    async def restore(self) -> None:
        now = self.clock()
        async with self.sm() as s:
            state = await repo.get_state(s, self._state_key) or {}
            self._screened_at = await repo.recent_screened(s, self.name, now - self.cfg.rescreen_after)
        self._last_run = {k: t for k, v in state.items() if (t := iso(v)) is not None}
        self._restored = True

    def _due(self, key: str, every: timedelta, now: datetime) -> bool:
        last = self._last_run.get(key)
        return last is None or now - last >= every

    def extra_rejections(self, snap: PoolSnapshot, now: datetime) -> list[str]:
        """Pipeline-specific checks after the profile screen. Empty list = pass."""
        return []

    # --- one cycle ------------------------------------------------------------
    async def gather(self, now: datetime, stats: PipelineStats) -> list[Lead]:
        leads: list[Lead] = []
        ran: dict[str, datetime] = {}
        gated_used = 0
        # Cheap sources first; among scarce ones, the most overdue goes first.
        jobs = []
        for src in self.sources:
            for chains in ([[c] for c in self.cfg.chains] if src.per_chain else [list(self.cfg.chains)]):
                key = f"{src.name}:{chains[0].value}" if src.per_chain else src.name
                if self._due(key, src.every, now):
                    never = datetime.min.replace(tzinfo=now.tzinfo)
                    jobs.append((getattr(src, "gated", False), self._last_run.get(key, never), key, src, chains))
        jobs.sort(key=lambda j: (j[0], j[1]))
        for gated, _, key, src, chains in jobs:
            if gated:
                if gated_used >= self.cfg.max_gated_per_cycle:
                    continue
                gated_used += 1
            try:
                leads.extend(await src.fetch(chains))
                ran[key] = now
            except ProviderError as exc:
                stats.source_errors.append(f"{key}: {exc}")          # retried next cycle
        if ran:
            self._last_run.update(ran)
            async with self.sm() as s:
                await repo.set_state(s, self._state_key, self._last_run)
                await s.commit()
        return leads

    async def run_once(self) -> PipelineStats:
        if not self._restored:
            await self.restore()
        now = self.clock()
        stats = PipelineStats()
        raw = await self.gather(now, stats)
        stats.leads = len(raw)

        uniq: dict[tuple[Chain, str], Lead] = {}
        for lead in raw:
            k = (lead.chain, lead.token)
            if k not in uniq or (uniq[k].snapshot is None and lead.snapshot is not None):
                uniq[k] = lead

        todo: dict[Chain, list[Lead]] = {}
        for (chain, token), lead in uniq.items():
            last = self._screened_at.get((chain, token))
            if last is not None and now - last < self.cfg.rescreen_after:
                stats.skipped_recent += 1
                continue
            todo.setdefault(chain, []).append(lead)

        async with self.sm() as session:
            capacity = self.cfg.max_watchlist - await repo.count_watching(session)
            for chain, leads in todo.items():
                watched = await repo.watched_pool_addresses(session, chain)
                try:
                    resolved = await self.resolver.get_tokens(chain, [l.token for l in leads])
                except ProviderError as exc:
                    stats.source_errors.append(f"resolve:{chain.value}: {exc}")
                    resolved = []
                for lead in leads:
                    snap = best_pool(resolved, lead.token, self.screening) or lead.snapshot
                    if snap is not None and classify(snap, now, self.screening) not in (self.profile, None):
                        stats.other_profile += 1          # the other pipeline owns this lead
                        continue
                    self._screened_at[(chain, lead.token)] = now
                    if snap is None:
                        stats.unresolved += 1
                        await repo.record_lead(session, chain=chain, token_address=lead.token, pool_address=None,
                                               source=lead.source, reason=lead.reason, pipeline=self.name,
                                               passed=None, rejections={"reasons": ["unresolved"]}, discovered_at=now)
                        continue
                    if snap.pool_address in watched:
                        continue
                    result = screen(snap, now, self.screening)
                    if result.passed:
                        extra = self.extra_rejections(snap, now)
                        if extra:
                            result = replace(result, passed=False, rejections=extra)
                    stats.screened += 1
                    pool_id = await repo.upsert_pool(session, snap)
                    await repo.record_lead(
                        session, chain=chain, token_address=lead.token, pool_address=snap.pool_address,
                        pool_id=pool_id, source=lead.source, reason=lead.reason, pipeline=self.name,
                        profile=result.profile.value if result.profile else None, passed=result.passed,
                        rejections={"reasons": result.rejections}, metrics=result.metrics, discovered_at=now)
                    if not result.passed:
                        stats.rejections.update(result.rejections)
                        continue
                    stats.passed += 1
                    if capacity <= 0:
                        stats.watchlist_full += 1
                        continue
                    if await repo.watch_pool(session, pool_id=pool_id, chain=chain,
                                             base_token_address=snap.base_token.address, profile=result.profile.value,
                                             discovered_via=lead.reason, pipeline=self.name, now=now):
                        stats.added += 1
                        capacity -= 1
                        watched.add(snap.pool_address)
                        await repo.insert_snapshot(session, pool_id, snap)
            await session.commit()

        cutoff = now - self.cfg.rescreen_after
        self._screened_at = {k: t for k, t in self._screened_at.items() if t >= cutoff}
        log.info("discovery_cycle", pipeline=self.name, leads=stats.leads, screened=stats.screened,
                 passed=stats.passed, added=stats.added, unresolved=stats.unresolved,
                 other_profile=stats.other_profile, skipped_recent=stats.skipped_recent,
                 watchlist_full=stats.watchlist_full, errors=stats.source_errors,
                 top_rejections=stats.rejections.most_common(5))
        return stats


class NewLaunchPipeline(DiscoveryPipeline):
    name = "new"
    profile = Profile.NEW


class EstablishedPipeline(DiscoveryPipeline):
    name = "established"
    profile = Profile.ESTABLISHED


class MomentumPipeline(DiscoveryPipeline):
    """Established tokens (older than the new-token window) that show unusual momentum
    right now. Feeds: trending lists. A lead joins the watchlist only if it passes the
    established screen *and* the momentum detector fires on its current, quality-checked
    snapshot. Momentum among already-watched pools is detected by the collector from
    collected metrics (see Collector.snapshot_once)."""
    name = "momentum"
    profile = Profile.ESTABLISHED

    def __init__(self, *args, momentum_cfg=None, **kwargs):
        super().__init__(*args, **kwargs)
        from app.intelligence.momentum import MomentumConfig
        self.momentum_cfg = momentum_cfg or MomentumConfig()

    def extra_rejections(self, snap: PoolSnapshot, now: datetime) -> list[str]:
        from app.intelligence.metrics import MetricsEngine
        from app.intelligence.momentum import is_momentum, triggers
        from app.quality.checks import check_snapshot
        q = check_snapshot(snap, now)
        if not q.reliable:
            return [f"quality:{f}" for f in q.flags]
        m = MetricsEngine().compute(snap, q)
        trig = triggers(m, "established", self.momentum_cfg)
        return [] if is_momentum(trig, self.momentum_cfg) else ["no_momentum"]
