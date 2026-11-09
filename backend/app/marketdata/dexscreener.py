"""DexScreener provider.

Endpoints per https://docs.dexscreener.com/api/reference (checked 2026-11-09):
  GET /latest/dex/pairs/{chainId}/{pairId}
  GET /latest/dex/search?q=
  GET /token-pairs/v1/{chainId}/{tokenAddress}
  GET /tokens/v1/{chainId}/{tokenAddresses}      (comma-separated)
  GET /token-profiles/latest/v1                    (60 req/min documented)
  GET /token-boosts/latest/v1, /token-boosts/top/v1
DexScreener offers no OHLCV, trade list, unique-trader counts or history.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.chains import (
    DEXSCREENER_CHAIN_IDS, Chain, InvalidAddress, chain_from_provider_id, normalize_address,
    normalize_pool_id,
)
from app.domain.market import (
    Candle, DataSource, DiscoveredToken, PoolSnapshot, PoolTrade, Timeframe, TokenRef, WindowStats,
)
from app.logging import get_logger
from app.marketdata.base import Capability
from app.marketdata.errors import CapabilityNotSupported, InvalidResponse
from app.marketdata.http import JsonHttpClient, ProviderStatus
from app.marketdata.parsing import dec, int_or_none, ts_ms, utcnow

log = get_logger(__name__)

# Max addresses per /tokens/v1 and multi-pair /latest/dex/pairs call. Commonly cited as 30;
# not stated in rendered docs. Comma-separated pair ids verified live on 2026-11-09.
TOKENS_BATCH = 30
PAIRS_BATCH = 30


class DexScreenerProvider:
    name = "dexscreener"
    capabilities = frozenset({
        Capability.POOL_SNAPSHOT, Capability.TOKEN_POOLS, Capability.SEARCH, Capability.TOKEN_PROFILES,
    })

    def __init__(self, client: JsonHttpClient, enabled_chains: list[Chain] | None = None):
        self.client = client
        self.enabled_chains = set(enabled_chains or list(Chain))

    async def aclose(self) -> None:
        await self.client.aclose()

    def status(self) -> ProviderStatus:
        return self.client.status()

    # --- parsing -------------------------------------------------------------
    def parse_pair(self, p: dict[str, Any], observed_at: datetime | None = None) -> PoolSnapshot | None:
        """Parse one DexScreener pair object. Returns None for unsupported chains or bad addresses."""
        if not isinstance(p, dict):
            raise InvalidResponse(self.name, "pair is not an object")
        chain = chain_from_provider_id(DEXSCREENER_CHAIN_IDS, p.get("chainId", ""))
        if chain is None or chain not in self.enabled_chains:
            return None
        try:
            pool_addr = normalize_pool_id(chain, p["pairAddress"])
            base = p["baseToken"]
            base_ref = TokenRef(chain, normalize_address(chain, base["address"]), base.get("symbol"), base.get("name"))
            q = p.get("quoteToken") or {}
            quote_ref = (TokenRef(chain, normalize_address(chain, q["address"]), q.get("symbol"), q.get("name"))
                         if q.get("address") else None)
        except (KeyError, TypeError, InvalidAddress) as exc:
            log.info("dexscreener_pair_rejected", reason=str(exc), pair=p.get("pairAddress"))
            return None

        txns = p.get("txns") or {}
        volume = p.get("volume") or {}
        change = p.get("priceChange") or {}
        windows: dict[str, WindowStats] = {}
        for key in set(txns) | set(volume) | set(change):
            t = txns.get(key) or {}
            windows[key] = WindowStats(
                buys=int_or_none(t.get("buys")), sells=int_or_none(t.get("sells")),
                volume_usd=dec(volume.get(key)), price_change_pct=dec(change.get(key)),
            )
        liq = p.get("liquidity") or {}
        return PoolSnapshot(
            source=DataSource.DEXSCREENER, chain=chain, pool_address=pool_addr, dex_id=p.get("dexId"),
            base_token=base_ref, quote_token=quote_ref, observed_at=observed_at or utcnow(),
            price_usd=dec(p.get("priceUsd")), price_native=dec(p.get("priceNative")),
            liquidity_usd=dec(liq.get("usd")), fdv_usd=dec(p.get("fdv")), market_cap_usd=dec(p.get("marketCap")),
            pool_created_at=ts_ms(p.get("pairCreatedAt")), windows=windows, raw=p,
        )

    def _parse_pairs(self, items: Any, observed_at: datetime | None = None) -> list[PoolSnapshot]:
        if items is None:
            return []
        if not isinstance(items, list):
            raise InvalidResponse(self.name, "expected a list of pairs")
        return [s for s in (self.parse_pair(i, observed_at) for i in items) if s is not None]

    # --- queries -------------------------------------------------------------
    async def get_pool(self, chain: Chain, pool_address: str) -> PoolSnapshot | None:
        addr = normalize_pool_id(chain, pool_address)
        res = await self.client.get_json_meta(
            f"/latest/dex/pairs/{DEXSCREENER_CHAIN_IDS[chain]}/{addr}", cache_ttl_s=10, allow_404=True)
        if not res.data:
            return None
        pools = self._parse_pairs(res.data.get("pairs"), res.fetched_at)
        return next((s for s in pools if s.pool_address == addr), None)

    async def get_pools(self, chain: Chain, pool_addresses: list[str]) -> dict[str, PoolSnapshot]:
        """Snapshots for many pools in batched calls, keyed by pool address. Pools the
        API does not return are simply absent (the caller decides what a miss means)."""
        addrs = list(dict.fromkeys(normalize_pool_id(chain, a) for a in pool_addresses))
        out: dict[str, PoolSnapshot] = {}
        for i in range(0, len(addrs), PAIRS_BATCH):
            chunk = addrs[i:i + PAIRS_BATCH]
            res = await self.client.get_json_meta(
                f"/latest/dex/pairs/{DEXSCREENER_CHAIN_IDS[chain]}/{','.join(chunk)}", cache_ttl_s=10,
                allow_404=True)
            wanted = set(chunk)
            for snap in self._parse_pairs((res.data or {}).get("pairs"), res.fetched_at):
                if snap.pool_address in wanted:
                    out[snap.pool_address] = snap
        return out

    async def get_token_pools(self, chain: Chain, token_address: str) -> list[PoolSnapshot]:
        addr = normalize_address(chain, token_address)
        res = await self.client.get_json_meta(
            f"/token-pairs/v1/{DEXSCREENER_CHAIN_IDS[chain]}/{addr}", cache_ttl_s=15, allow_404=True)
        return self._parse_pairs(res.data, res.fetched_at)

    async def get_tokens(self, chain: Chain, token_addresses: list[str]) -> list[PoolSnapshot]:
        """Pools for many tokens in batched calls."""
        addrs = [normalize_address(chain, a) for a in token_addresses]
        out: list[PoolSnapshot] = []
        for i in range(0, len(addrs), TOKENS_BATCH):
            chunk = ",".join(addrs[i:i + TOKENS_BATCH])
            res = await self.client.get_json_meta(
                f"/tokens/v1/{DEXSCREENER_CHAIN_IDS[chain]}/{chunk}", cache_ttl_s=10, allow_404=True)
            out.extend(self._parse_pairs(res.data, res.fetched_at))
        return out

    async def search(self, query: str) -> list[PoolSnapshot]:
        res = await self.client.get_json_meta("/latest/dex/search", {"q": query}, cache_ttl_s=30)
        return self._parse_pairs((res.data or {}).get("pairs"), res.fetched_at)

    async def discover(self, chains: list[Chain],
                       kinds: tuple[str, ...] = ("profiles", "boosts")) -> list[DiscoveredToken]:
        """Leads from latest token profiles and boosts. Boosts are paid promotion and are
        labelled as such so screening can treat them sceptically."""
        wanted = {DEXSCREENER_CHAIN_IDS[c]: c for c in chains if c in self.enabled_chains}
        leads: list[DiscoveredToken] = []
        endpoints = {"profiles": ("/token-profiles/latest/v1", "dexscreener_profile"),
                     "boosts": ("/token-boosts/latest/v1", "dexscreener_boost_paid")}
        for path, reason in (endpoints[k] for k in kinds):
            res = await self.client.get_json_meta(path, cache_ttl_s=30, limiter="profiles")
            data = res.data
            if not isinstance(data, list):
                raise InvalidResponse(self.name, f"{path} did not return a list")
            for item in data:
                chain = wanted.get(item.get("chainId")) if isinstance(item, dict) else None
                if chain is None:
                    continue
                try:
                    addr = normalize_address(chain, item.get("tokenAddress", ""))
                except InvalidAddress:
                    continue
                leads.append(DiscoveredToken(DataSource.DEXSCREENER, chain, addr, None, res.fetched_at, reason))
        return leads

    async def get_new_pools(self, chain: Chain) -> list[PoolSnapshot]:
        raise CapabilityNotSupported(self.name, "new_pools")

    async def get_trending_pools(self, chain: Chain) -> list[PoolSnapshot]:
        raise CapabilityNotSupported(self.name, "trending_pools")

    async def get_ohlcv(self, chain: Chain, pool_address: str, timeframe: Timeframe,
                        limit: int = 100, before_ts: int | None = None) -> list[Candle]:
        raise CapabilityNotSupported(self.name, "ohlcv")

    async def get_trades(self, chain: Chain, pool_address: str) -> list[PoolTrade]:
        raise CapabilityNotSupported(self.name, "trades")
