"""GeckoTerminal provider (keyless public API).

Endpoints per CoinGecko onchain docs (checked 2026-11-09), base https://api.geckoterminal.com/api/v2:
  GET /networks/{network}/pools/{address}
  GET /networks/{network}/new_pools
  GET /networks/{network}/trending_pools
  GET /networks/{network}/pools/{pool}/ohlcv/{day|hour|minute|second}
      ?aggregate=&before_timestamp=&limit(<=1000)&currency=usd&token=base
  GET /networks/{network}/pools/{pool}/trades
Keyless rate limit is roughly 5 calls/min per IP and varies with load.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.chains import (
    GECKOTERMINAL_NETWORK_IDS, Chain, InvalidAddress, chain_from_provider_id, normalize_address,
    normalize_pool_id,
)
from app.domain.market import (
    WINDOWS, Candle, DataSource, DiscoveredToken, PoolSnapshot, PoolTrade, Timeframe, TokenRef,
    TradeKind, WindowStats,
)
from app.logging import get_logger
from app.marketdata.base import Capability
from app.marketdata.errors import CapabilityNotSupported, InvalidResponse
from app.marketdata.http import JsonHttpClient, ProviderStatus
from app.marketdata.parsing import dec, int_or_none, iso, utcnow

log = get_logger(__name__)

OHLCV_PARAMS: dict[Timeframe, tuple[str, int]] = {
    Timeframe.S1: ("second", 1), Timeframe.S15: ("second", 15), Timeframe.S30: ("second", 30),
    Timeframe.M1: ("minute", 1), Timeframe.M5: ("minute", 5), Timeframe.M15: ("minute", 15),
    Timeframe.H1: ("hour", 1), Timeframe.H4: ("hour", 4), Timeframe.H12: ("hour", 12),
    Timeframe.D1: ("day", 1),
}
OHLCV_MAX_LIMIT = 1000
MULTI_BATCH = 30


class GeckoTerminalProvider:
    name = "geckoterminal"
    capabilities = frozenset({
        Capability.POOL_SNAPSHOT, Capability.NEW_POOLS, Capability.TRENDING_POOLS,
        Capability.OHLCV, Capability.TRADES, Capability.UNIQUE_TRADERS,
    })

    def __init__(self, client: JsonHttpClient, enabled_chains: list[Chain] | None = None):
        self.client = client
        self.enabled_chains = set(enabled_chains or list(Chain))

    async def aclose(self) -> None:
        await self.client.aclose()

    def status(self) -> ProviderStatus:
        return self.client.status()

    @staticmethod
    def _network(chain: Chain) -> str:
        return GECKOTERMINAL_NETWORK_IDS[chain]

    def _rel_address(self, network: str, rel: dict | None) -> str | None:
        rid = (((rel or {}).get("data") or {}).get("id")) or ""
        prefix = f"{network}_"
        return rid[len(prefix):] if rid.startswith(prefix) else None

    # --- parsing -------------------------------------------------------------
    def parse_pool(self, item: dict[str, Any], chain: Chain, observed_at: datetime | None = None) -> PoolSnapshot | None:
        if not isinstance(item, dict) or item.get("type") != "pool":
            raise InvalidResponse(self.name, "expected a pool resource")
        network = self._network(chain)
        a = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        name = a.get("name") or ""
        base_sym, _, quote_sym = name.partition(" / ")
        try:
            pool_addr = normalize_pool_id(chain, a.get("address", ""))
            base_addr = self._rel_address(network, rel.get("base_token"))
            if base_addr is None:
                raise InvalidAddress("missing base token")
            base_ref = TokenRef(chain, normalize_address(chain, base_addr), base_sym or None)
            quote_addr = self._rel_address(network, rel.get("quote_token"))
            quote_ref = TokenRef(chain, normalize_address(chain, quote_addr), quote_sym or None) if quote_addr else None
        except InvalidAddress as exc:
            log.info("geckoterminal_pool_rejected", reason=str(exc), pool=a.get("address"))
            return None

        txs = a.get("transactions") or {}
        vols = a.get("volume_usd") or {}
        changes = a.get("price_change_percentage") or {}
        windows: dict[str, WindowStats] = {}
        for key in WINDOWS:
            if key not in txs and key not in vols and key not in changes:
                continue
            t = txs.get(key) or {}
            windows[key] = WindowStats(
                buys=int_or_none(t.get("buys")), sells=int_or_none(t.get("sells")),
                buyers=int_or_none(t.get("buyers")), sellers=int_or_none(t.get("sellers")),
                volume_usd=dec(vols.get(key)), price_change_pct=dec(changes.get(key)),
            )
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
        return PoolSnapshot(
            source=DataSource.GECKOTERMINAL, chain=chain, pool_address=pool_addr, dex_id=dex,
            base_token=base_ref, quote_token=quote_ref, observed_at=observed_at or utcnow(),
            price_usd=dec(a.get("base_token_price_usd")), price_native=dec(a.get("base_token_price_quote_token")),
            liquidity_usd=dec(a.get("reserve_in_usd")), fdv_usd=dec(a.get("fdv_usd")),
            market_cap_usd=dec(a.get("market_cap_usd")), pool_created_at=iso(a.get("pool_created_at")),
            windows=windows, locked_liquidity_pct=dec(a.get("locked_liquidity_percentage")),
            pool_fee_pct=dec(a.get("pool_fee_percentage")), raw=item,
        )

    def _parse_pool_list(self, data: Any, chain: Chain, observed_at: datetime | None = None) -> list[PoolSnapshot]:
        items = (data or {}).get("data")
        if not isinstance(items, list):
            raise InvalidResponse(self.name, "expected data[] of pools")
        return [s for s in (self.parse_pool(i, chain, observed_at) for i in items) if s is not None]

    @staticmethod
    def parse_ohlcv(data: Any, timeframe: Timeframe, now: datetime | None = None) -> list[Candle]:
        try:
            rows = data["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as exc:
            raise InvalidResponse("geckoterminal", "missing ohlcv_list") from exc
        now = now or datetime.now(timezone.utc)
        span = timedelta(seconds=timeframe.seconds)
        candles: dict[int, Candle] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                continue
            ts = int_or_none(row[0])
            o, h, l, c, v = (dec(x) for x in row[1:])
            if ts is None or None in (o, h, l, c, v) or h < l or v < 0:
                continue  # drop malformed candles rather than guess
            open_time = datetime.fromtimestamp(ts, tz=timezone.utc)
            candles[ts] = Candle(open_time, timeframe, o, h, l, c, v, is_closed=open_time + span <= now)
        return [candles[k] for k in sorted(candles)]  # oldest first

    # --- queries -------------------------------------------------------------
    async def get_pool(self, chain: Chain, pool_address: str) -> PoolSnapshot | None:
        addr = normalize_pool_id(chain, pool_address)
        res = await self.client.get_json_meta(f"/networks/{self._network(chain)}/pools/{addr}",
                                              cache_ttl_s=30, allow_404=True)
        if not res.data:
            return None
        item = res.data.get("data")
        return self.parse_pool(item, chain, res.fetched_at) if item else None

    async def get_new_pools(self, chain: Chain) -> list[PoolSnapshot]:
        res = await self.client.get_json_meta(f"/networks/{self._network(chain)}/new_pools", {"page": 1},
                                              cache_ttl_s=60)
        return self._parse_pool_list(res.data, chain, res.fetched_at)

    async def get_trending_pools(self, chain: Chain) -> list[PoolSnapshot]:
        res = await self.client.get_json_meta(f"/networks/{self._network(chain)}/trending_pools", {"page": 1},
                                              cache_ttl_s=120)
        return self._parse_pool_list(res.data, chain, res.fetched_at)

    async def get_pools_multi(self, chain: Chain, pool_addresses: list[str]) -> dict[str, PoolSnapshot]:
        """Up to 30 pools per call via /networks/{network}/pools/multi/{addresses}
        (verified keyless on 2026-11-09). Source of unique buyer/seller counts."""
        addrs = list(dict.fromkeys(normalize_pool_id(chain, a) for a in pool_addresses))
        out: dict[str, PoolSnapshot] = {}
        for i in range(0, len(addrs), MULTI_BATCH):
            chunk = addrs[i:i + MULTI_BATCH]
            res = await self.client.get_json_meta(
                f"/networks/{self._network(chain)}/pools/multi/{','.join(chunk)}", cache_ttl_s=30, allow_404=True)
            if not res.data:
                continue
            for snap in self._parse_pool_list(res.data, chain, res.fetched_at):
                if snap.pool_address in chunk:
                    out[snap.pool_address] = snap
        return out

    async def get_top_pools(self, chain: Chain) -> list[PoolSnapshot]:
        """Top pools of a network by 24h volume: /networks/{network}/pools?sort=h24_volume_usd_desc.
        Used to seed the established-token universe."""
        res = await self.client.get_json_meta(f"/networks/{self._network(chain)}/pools",
                                              {"page": 1, "sort": "h24_volume_usd_desc"}, cache_ttl_s=600)
        return self._parse_pool_list(res.data, chain, res.fetched_at)

    async def discover(self, chains: list[Chain]) -> list[DiscoveredToken]:
        leads: list[DiscoveredToken] = []
        for chain in chains:
            if chain not in self.enabled_chains:
                continue
            for snap in await self.get_new_pools(chain):
                leads.append(DiscoveredToken(DataSource.GECKOTERMINAL, chain, snap.base_token.address,
                                             snap.pool_address, utcnow(), "geckoterminal_new_pool"))
        return leads

    async def get_ohlcv(self, chain: Chain, pool_address: str, timeframe: Timeframe,
                        limit: int = 100, before_ts: int | None = None) -> list[Candle]:
        addr = normalize_pool_id(chain, pool_address)
        unit, agg = OHLCV_PARAMS[timeframe]
        params: dict[str, Any] = {"aggregate": agg, "limit": max(1, min(limit, OHLCV_MAX_LIMIT)),
                                  "currency": "usd", "token": "base"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        data = await self.client.get_json(f"/networks/{self._network(chain)}/pools/{addr}/ohlcv/{unit}",
                                          params, cache_ttl_s=30, allow_404=True)
        return self.parse_ohlcv(data, timeframe) if data else []

    async def get_trades(self, chain: Chain, pool_address: str) -> list[PoolTrade]:
        addr = normalize_pool_id(chain, pool_address)
        data = await self.client.get_json(f"/networks/{self._network(chain)}/pools/{addr}/trades",
                                          cache_ttl_s=30, allow_404=True)
        items = (data or {}).get("data") or []
        out: list[PoolTrade] = []
        for it in items:
            a = (it or {}).get("attributes") or {}
            kind = a.get("kind")
            when = iso(a.get("block_timestamp"))
            if kind not in ("buy", "sell") or when is None or not a.get("tx_hash"):
                continue
            out.append(PoolTrade(chain, addr, a["tx_hash"], when, TradeKind(kind), a.get("tx_from_address"),
                                 dec(a.get("volume_in_usd")), int_or_none(a.get("block_number"))))
        return out

    async def get_token_pools(self, chain: Chain, token_address: str) -> list[PoolSnapshot]:
        raise CapabilityNotSupported(self.name, "token_pools (not implemented for keyless tier)")
