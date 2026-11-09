"""Market data service: routes requests to capable providers with failover.

Callers depend on this service, never on a concrete provider.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Awaitable, Callable, TypeVar

from app.config import Settings
from app.domain.chains import Chain
from app.domain.market import Candle, DiscoveredToken, PoolSnapshot, PoolTrade, Timeframe, WindowStats
from app.logging import get_logger
from app.marketdata.base import Capability, MarketDataProvider
from app.marketdata.dexscreener import DexScreenerProvider
from app.marketdata.errors import CapabilityNotSupported, InvalidResponse, ProviderError, ProviderUnavailable
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.http import CircuitBreaker, JsonHttpClient, MemoryTTLCache, RateLimiter

log = get_logger(__name__)
T = TypeVar("T")


def merge_snapshots(primary: PoolSnapshot, secondary: PoolSnapshot) -> PoolSnapshot:
    """Fill unknown (None) fields of `primary` from `secondary` for the same pool.

    Known values in `primary` are never overwritten, so the merge never hides a
    disagreement; use `price_divergence` to detect one.
    """
    if (primary.chain, primary.pool_address) != (secondary.chain, secondary.pool_address):
        raise ValueError("cannot merge snapshots of different pools")
    windows: dict[str, WindowStats] = {}
    for key in set(primary.windows) | set(secondary.windows):
        p, s = primary.window(key), secondary.window(key)
        windows[key] = WindowStats(**{f: getattr(p, f) if getattr(p, f) is not None else getattr(s, f)
                                      for f in WindowStats.__slots__})
    fill = {f: getattr(secondary, f) for f in (
        "price_usd", "price_native", "liquidity_usd", "fdv_usd", "market_cap_usd", "pool_created_at",
        "locked_liquidity_pct", "pool_fee_pct", "dex_id") if getattr(primary, f) is None}
    return replace(primary, windows=windows, **fill)


def price_divergence(a: PoolSnapshot, b: PoolSnapshot) -> Decimal | None:
    """Relative USD price difference between two sources, or None if unknown."""
    if a.price_usd is None or b.price_usd is None or min(a.price_usd, b.price_usd) <= 0:
        return None
    return abs(a.price_usd - b.price_usd) / min(a.price_usd, b.price_usd)


class MarketDataService:
    def __init__(self, providers: list[MarketDataProvider]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers

    def _capable(self, cap: Capability) -> list[MarketDataProvider]:
        return [p for p in self.providers if cap in p.capabilities]

    async def _first(self, cap: Capability, call: Callable[[MarketDataProvider], Awaitable[T]]) -> T:
        errors: list[str] = []
        for provider in self._capable(cap):
            try:
                return await call(provider)
            except (ProviderUnavailable, InvalidResponse, CapabilityNotSupported) as exc:
                errors.append(str(exc))
                log.warning("provider_failover", capability=cap, provider=provider.name, error=str(exc))
        raise ProviderUnavailable("aggregator", f"no provider could serve {cap}: {errors or 'none capable'}")

    async def get_pool(self, chain: Chain, pool_address: str, *, enrich: bool = False) -> PoolSnapshot | None:
        snap = await self._first(Capability.POOL_SNAPSHOT, lambda p: p.get_pool(chain, pool_address))
        if snap is None or not enrich:
            return snap
        return await self.enrich(snap)

    async def enrich(self, snap: PoolSnapshot) -> PoolSnapshot:
        """Fill gaps (e.g. unique buyers) from other providers. Best effort: failures leave
        fields unknown rather than raising."""
        for provider in self._capable(Capability.POOL_SNAPSHOT):
            if provider.name == snap.source.value:
                continue
            try:
                other = await provider.get_pool(snap.chain, snap.pool_address)
            except ProviderError as exc:
                log.info("enrich_skipped", provider=provider.name, error=str(exc))
                continue
            if other is not None:
                div = price_divergence(snap, other)
                if div is not None and div > Decimal("0.05"):
                    log.warning("price_divergence", pool=snap.pool_address, chain=snap.chain,
                                divergence=str(div), sources=[snap.source, other.source])
                snap = merge_snapshots(snap, other)
        return snap

    async def get_token_pools(self, chain: Chain, token_address: str) -> list[PoolSnapshot]:
        return await self._first(Capability.TOKEN_POOLS, lambda p: p.get_token_pools(chain, token_address))

    async def get_new_pools(self, chain: Chain) -> list[PoolSnapshot]:
        return await self._first(Capability.NEW_POOLS, lambda p: p.get_new_pools(chain))

    async def get_trending_pools(self, chain: Chain) -> list[PoolSnapshot]:
        return await self._first(Capability.TRENDING_POOLS, lambda p: p.get_trending_pools(chain))

    async def get_ohlcv(self, chain: Chain, pool_address: str, timeframe: Timeframe, limit: int = 100,
                        before_ts: int | None = None, closed_only: bool = True) -> list[Candle]:
        candles = await self._first(Capability.OHLCV, lambda p: p.get_ohlcv(chain, pool_address, timeframe,
                                                                             limit, before_ts))
        # The newest candle is usually still forming; strategies must not see it as final.
        return [c for c in candles if c.is_closed] if closed_only else candles

    async def get_trades(self, chain: Chain, pool_address: str) -> list[PoolTrade]:
        return await self._first(Capability.TRADES, lambda p: p.get_trades(chain, pool_address))

    async def discover(self, chains: list[Chain]) -> list[DiscoveredToken]:
        """Union of discovery leads from every provider, de-duplicated per (chain, token).
        One provider failing does not stop discovery from the others."""
        seen: dict[tuple[Chain, str], DiscoveredToken] = {}
        for provider in self.providers:
            try:
                leads = await provider.discover(chains)
            except (ProviderError,) as exc:
                log.warning("discovery_provider_failed", provider=provider.name, error=str(exc))
                continue
            for lead in leads:
                seen.setdefault((lead.chain, lead.token_address), lead)
        return list(seen.values())

    def health(self) -> list[dict]:
        return [vars(p.status()) for p in self.providers]

    async def aclose(self) -> None:
        for p in self.providers:
            await p.aclose()


def build_market_data_service(settings: Settings) -> MarketDataService:
    cache = MemoryTTLCache()
    ds_client = JsonHttpClient(
        "dexscreener", settings.dexscreener_base_url,
        limiters={"default": RateLimiter(settings.dexscreener_pairs_rpm, 60, burst=10),
                  "profiles": RateLimiter(settings.dexscreener_profiles_rpm, 60, burst=3)},
        cache=cache, timeout_s=settings.http_timeout_s, max_retries=settings.http_max_retries,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
    )
    gt_client = JsonHttpClient(
        "geckoterminal", settings.geckoterminal_base_url,
        limiters={"default": RateLimiter(settings.geckoterminal_rpm, 60, burst=1)},
        cache=cache, timeout_s=settings.http_timeout_s, max_retries=settings.http_max_retries,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
    )
    chains = settings.enabled_chains
    return MarketDataService([DexScreenerProvider(ds_client, chains), GeckoTerminalProvider(gt_client, chains)])
