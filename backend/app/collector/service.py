"""One collector cycle. DexScreener does the bulk reads. GeckoTerminal is optional and slow.

Unique buyers exist only on GeckoTerminal. They are requested for the 15, 60 and 240 minute
slots, and only when the collector's own GeckoTerminal interval has elapsed, so this process
does not consume the shared ~5 calls/minute budget by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal

from app.collector.schedule import BUYER_AGES, SCREEN_AGES, catch_up_limit, classify, partition
from app.collector.store import (
    Candidate, add_missed, add_observation, advance, due_watches, enroll, taken_ages,
)
from app.config import Settings
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.logging import get_logger
from app.marketdata.errors import ProviderError, UnsupportedPoolId
from app.marketdata.parsing import dec
from app.screening.engine import ScreeningEngine
from app.screening.filters import is_stable, quote_allowed
from app.screening.models import ScreenDecision

WATCH_CHAINS = (Chain.SOLANA, Chain.BASE)
MAX_ENROLL_AGE = timedelta(minutes=30)
log = get_logger(__name__)


@dataclass
class CollectorState:
    last_gt_at: datetime | None = None
    chain_cursor: int = 0
    gt_calls: int = 0
    enrolled: int = 0
    observed: int = 0
    missed: int = 0
    screens: int = 0
    skipped_unsupported: int = 0
    clock_jumped: bool = False
    last_wall: datetime | None = None
    stalls: int = 0


def snapshot_fields(snap: PoolSnapshot, buyers: PoolSnapshot | None = None) -> dict:
    """Copy the fields we keep. The provider payload itself is not stored."""
    m5, h1, h24 = snap.window("m5"), snap.window("h1"), snap.window("h24")
    buyers_m5 = sellers_m5 = buyers_h1 = sellers_h1 = None
    source = snap.source.value
    if buyers is not None:
        b5, b1 = buyers.window("m5"), buyers.window("h1")
        buyers_m5, sellers_m5 = b5.buyers, b5.sellers
        buyers_h1, sellers_h1 = b1.buyers, b1.sellers
        if any(v is not None for v in (buyers_m5, sellers_m5, buyers_h1, sellers_h1)):
            source = f"{source}+{buyers.source.value}"
    base_reserve, quote_reserve = _reserves(snap)
    return {
        "source": source,
        "price_usd": snap.price_usd,
        "liquidity_usd": snap.liquidity_usd,
        "fdv_usd": snap.fdv_usd,
        "market_cap_usd": snap.market_cap_usd,
        "base_reserve": base_reserve,
        "quote_reserve": quote_reserve,
        "volume_m5": m5.volume_usd,
        "volume_h1": h1.volume_usd,
        "volume_h24": h24.volume_usd,
        "buys_m5": m5.buys,
        "sells_m5": m5.sells,
        "buys_h1": h1.buys,
        "sells_h1": h1.sells,
        "buyers_m5": buyers_m5,
        "sellers_m5": sellers_m5,
        "buyers_h1": buyers_h1,
        "sellers_h1": sellers_h1,
    }


def _reserves(snap: PoolSnapshot):
    raw = snap.raw if isinstance(snap.raw, dict) else {}
    liquidity = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}
    return dec(liquidity.get("base")), dec(liquidity.get("quote"))


def screen_document(decision: ScreenDecision) -> dict:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason[:500],
        "checks": [
            {"name": check.name, "status": check.status.value, "critical": check.critical}
            for check in decision.checks[:24]
        ],
    }


def _candidate(snap: PoolSnapshot, now: datetime) -> Candidate | None:
    if snap.chain not in WATCH_CHAINS:
        return None
    quote = snap.quote_token.address if snap.quote_token else None
    if quote is not None and not quote_allowed(snap.chain, quote):
        return None
    if is_stable(snap.chain, snap.base_token.address):
        return None
    return Candidate(
        chain=snap.chain, pool_address=snap.pool_address, token_address=snap.base_token.address,
        quote_address=quote, symbol=snap.base_token.symbol, dex_id=snap.dex_id,
        pool_created_at=snap.pool_created_at,
    )


async def _enroll_snap(session, snap: PoolSnapshot, *, now: datetime, settings: Settings, state: CollectorState) -> None:
    candidate = _candidate(snap, now)
    if candidate is None:
        return
    created = await enroll(
        session, candidate, now=now, max_active=settings.collector_max_active,
        max_per_day=settings.collector_max_new_per_day, max_age=MAX_ENROLL_AGE,
    )
    if created:
        state.enrolled += 1


async def discover(session, ds, gt, *, now: datetime, settings: Settings, state: CollectorState) -> None:
    gt_due = state.last_gt_at is None or (now - state.last_gt_at).total_seconds() >= settings.collector_gt_interval_s
    if gt_due:
        chain = WATCH_CHAINS[state.chain_cursor % len(WATCH_CHAINS)]
        state.chain_cursor += 1
        try:
            pools = await gt.get_new_pools(chain)
        except ProviderError:
            pools = []
        state.last_gt_at = now
        state.gt_calls += 1
        for snap in pools:
            await _enroll_snap(session, snap, now=now, settings=settings, state=state)
    try:
        leads = await ds.discover(list(WATCH_CHAINS))
    except ProviderError:
        leads = []
    resolved = 0
    for lead in leads:
        if resolved >= 3 or lead.chain not in WATCH_CHAINS:
            continue
        if lead.pool_address is not None:
            continue
        try:
            pools = await ds.get_token_pools(lead.chain, lead.token_address)
        except ProviderError:
            continue
        resolved += 1
        pools.sort(key=lambda snap: snap.liquidity_usd or Decimal(0), reverse=True)
        if pools:
            await _enroll_snap(session, pools[0], now=now, settings=settings, state=state)


async def observe(session: AsyncSession, ds, gt, screening: ScreeningEngine | None, *, now: datetime,
                  settings: Settings, state: CollectorState) -> None:
    watches = await due_watches(
        session, now, catch_up_limit(state.clock_jumped, settings.collector_snapshots_per_cycle),
    )
    for watch in watches:
        done = await taken_ages(session, watch.id)
        missed, age = partition(watch.launch_at, now, done)
        for missed_age in missed:
            await add_missed(session, watch, missed_age)
            state.missed += 1
        if age is None or classify(watch.launch_at, age, now) != "due":
            await advance(session, watch, now)
            continue
        try:
            snap = await ds.get_pool(watch.chain, watch.pool_address)
        except UnsupportedPoolId:
            state.skipped_unsupported += 1
            log.info("skip_pool", reason="unsupported_pool_id", chain=watch.chain.value, pool=watch.pool_address)
            await add_observation(
                session, watch, age=age, now=now, source="unsupported_pool_id", not_found=False, fields={}, screen=None,
            )
            watch.last_error = "unsupported_pool_id"
            await session.flush()
            await advance(session, watch, now)
            continue
        except ProviderError as exc:
            watch.last_error = str(exc)[:300]
            continue
        buyers = None
        gt_free = state.last_gt_at is None or (now - state.last_gt_at).total_seconds() >= settings.collector_gt_interval_s
        if snap is not None and age in BUYER_AGES and gt_free:
            try:
                buyers = await gt.get_pool(watch.chain, watch.pool_address)
                state.last_gt_at = now
                state.gt_calls += 1
            except UnsupportedPoolId:
                state.skipped_unsupported += 1
                log.info("skip_pool", reason="unsupported_pool_id", chain=watch.chain.value, pool=watch.pool_address)
                buyers = None
            except ProviderError:
                buyers = None
        screen = None
        first_real = watch.snapshots_taken == 0
        if screening is not None and snap is not None and (age in SCREEN_AGES or first_real) and state.screens < settings.collector_screens_per_cycle:
            try:
                decision = await screening.screen(snap, now=now, with_security=True)
                screen = screen_document(decision)
                state.screens += 1
                if any(check.get("name") == "unsupported_pool_id" for check in screen["checks"]):
                    state.skipped_unsupported += 1
            except Exception:
                screen = None
        if snap is None:
            await add_observation(
                session, watch, age=age, now=now, source="dexscreener", not_found=True, fields={}, screen=None,
            )
        else:
            fields = snapshot_fields(snap, buyers)
            await add_observation(
                session, watch, age=age, now=now, source=fields.pop("source"), not_found=False, fields=fields, screen=screen,
            )
        state.observed += 1
        watch.last_error = None
        await session.flush()
        await advance(session, watch, now)


@dataclass
class CycleResult:
    state: CollectorState = field(default_factory=CollectorState)


async def run_cycle(session, ds, gt, screening, settings: Settings, state: CollectorState, now: datetime | None = None) -> CollectorState:
    now = now or datetime.now(timezone.utc)
    await discover(session, ds, gt, now=now, settings=settings, state=state)
    await observe(session, ds, gt, screening, now=now, settings=settings, state=state)
    return state
