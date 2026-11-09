"""Point-in-time launch collector.

    python -m app.collector --once
    python -m app.collector
"""
from __future__ import annotations

import argparse
import asyncio

from app.collector.service import CollectorState, run_cycle
from app.config import TradingMode, get_settings
from app.domain.chains import Chain
from app.logging import configure_logging, get_logger
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.http import CircuitBreaker, JsonHttpClient, MemoryTTLCache, RateLimiter
from app.paper.repository import session_scope
from app.screening.engine import ScreeningEngine
from app.screening.filters import thresholds_from_settings
from app.screening.providers import build_security_clients

log = get_logger(__name__)


def _clients(settings):
    cache = MemoryTTLCache(max_entries=100)
    ds = JsonHttpClient(
        "dexscreener", settings.dexscreener_base_url,
        limiters={"default": RateLimiter(settings.dexscreener_pairs_rpm, 60, burst=5),
                  "profiles": RateLimiter(settings.dexscreener_profiles_rpm, 60, burst=2)},
        cache=cache, timeout_s=settings.http_timeout_s, max_retries=2,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
    )
    gt = JsonHttpClient(
        "geckoterminal", settings.geckoterminal_base_url,
        limiters={"default": RateLimiter(1, settings.collector_gt_interval_s, burst=1)},
        rate_limit_penalty_s=30.0, cache=cache, timeout_s=settings.http_timeout_s, max_retries=1,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
    )
    chains = [Chain.SOLANA, Chain.BASE]
    return DexScreenerProvider(ds, chains), GeckoTerminalProvider(gt, chains)


async def _once(state: CollectorState) -> CollectorState:
    settings = get_settings()
    if settings.trading_mode is not TradingMode.PAPER:
        raise SystemExit("refusing to start: trading mode is not paper")
    ds, gt = _clients(settings)
    security = build_security_clients()
    screening = ScreeningEngine(security, thresholds_from_settings(settings))
    try:
        await _cycle(ds, gt, screening, state)
    finally:
        await screening.aclose()
        await ds.aclose()
        await gt.aclose()
    return state


async def _cycle(ds, gt, screening, state: CollectorState) -> None:
    settings = get_settings()
    async with session_scope(settings.database_url) as session:
        async with session.begin():
            await run_cycle(session, ds, gt, screening, settings, state)
    log.info("collector_cycle", enrolled=state.enrolled, observed=state.observed, missed=state.missed,
             screens=state.screens, gt_calls=state.gt_calls)


async def _loop() -> None:
    settings = get_settings()
    if settings.trading_mode is not TradingMode.PAPER:
        raise SystemExit("refusing to start: trading mode is not paper")
    state = CollectorState()
    ds, gt = _clients(settings)
    security = build_security_clients()
    screening = ScreeningEngine(security, thresholds_from_settings(settings))
    try:
        while True:
            try:
                await _cycle(ds, gt, screening, state)
            except Exception as exc:
                log.error("collector_cycle_failed", error=type(exc).__name__)
            await asyncio.sleep(settings.collector_poll_s)
    finally:
        await screening.aclose()
        await ds.aclose()
        await gt.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record point-in-time snapshots of new Solana and Base pools. Does not trade.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, False)
    print("DexBot launch collector. Paper-only. No signing or live orders.")
    if args.once:
        asyncio.run(_once(CollectorState()))
        return
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
