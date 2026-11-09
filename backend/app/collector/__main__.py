"""Run the collector: python -m app.collector"""
from __future__ import annotations

import asyncio
import signal

from app.collector.worker import Collector, CollectorConfig
from app.config import get_settings
from app.db.session import make_engine, make_sessionmaker
from datetime import timedelta

from app.discovery.pipelines import EstablishedPipeline, MomentumPipeline, NewLaunchPipeline, PipelineConfig
from app.discovery.screening import ScreeningConfig
from app.discovery.sources import DexScreenerFeed, GeckoPoolFeed, QuoteSeedFeed
from app.discovery.validation import validate_quote_assets
from app.logging import configure_logging, get_logger
from app.marketdata.aggregator import build_market_data_service
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import ProviderError
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.scheduling import PriorityGate

log = get_logger("collector")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    market = build_market_data_service(settings)
    ds = next(p for p in market.providers if isinstance(p, DexScreenerProvider))
    gt = next(p for p in market.providers if isinstance(p, GeckoTerminalProvider))
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Hardcoded quote-asset addresses are trusted only after a live symbol check.
    verified = None
    while verified is None and not stop.is_set():
        try:
            verified = await validate_quote_assets(ds, settings.enabled_chains)
        except ProviderError as exc:
            log.warning("quote_validation_retry", error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), 30)
            except asyncio.TimeoutError:
                pass
    if verified is not None:
        log.info("quote_assets_verified", **{c.value: len(v) for c, v in verified.items()})
        gate = PriorityGate()
        screening = ScreeningConfig(verified_quotes=verified)
        pcfg = PipelineConfig(chains=tuple(settings.enabled_chains), max_watchlist=settings.collector_max_watchlist)
        trending = GeckoPoolFeed(gt, gate, "trending", timedelta(minutes=15))
        pipelines = [
            NewLaunchPipeline([GeckoPoolFeed(gt, gate, "new_pools", timedelta(minutes=5)),
                               DexScreenerFeed(ds, "profiles"), DexScreenerFeed(ds, "boosts"), trending],
                              ds, sm, screening, pcfg),
            EstablishedPipeline([QuoteSeedFeed(verified), GeckoPoolFeed(gt, gate, "top_pools", timedelta(hours=1))],
                                ds, sm, screening, pcfg),
            # Trending responses are cached for 2 minutes, so this feed usually costs no extra call.
            MomentumPipeline([trending], ds, sm, screening, pcfg),
        ]
        collector = Collector(ds, gt, gate, sm, pipelines, CollectorConfig())
        log.info("collector_started", chains=[c.value for c in settings.enabled_chains], mode=settings.trading_mode)
        await collector.run(stop)
    await market.aclose()
    await engine.dispose()
    log.info("collector_stopped")


if __name__ == "__main__":
    asyncio.run(main())
