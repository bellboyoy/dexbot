"""Collect a Solana + Base sample that is not just today's trending winners.

GeckoTerminal's keyless `new_pools` pages, checked 2026-11-09, only cover pools created
in the last few minutes, and pages past 10 on `/pools` return 401. There is no historical
"new pools as of date X" endpoint, and OHLCV rows are `[ts, o, h, l, c, volume]` with no
reserve. Liquidity stored here is the spot snapshot. Pools with little liquidity and an
age of at least a few hours are the failed-launch cohort; high-liquidity older pools are
the survivors. Both are kept.

Candles are cached under data/market_cache so a second run does not spend the rate limit.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.costs import fee_rate_for_snapshot
from app.domain.chains import Chain, normalize_pool_id
from app.domain.market import DataSource, PoolSnapshot, Timeframe, TokenRef
from app.marketdata.errors import ProviderError
from app.marketdata.geckoterminal import GeckoTerminalProvider
from app.marketdata.http import CircuitBreaker, JsonHttpClient, RateLimiter
from app.research.cache import read_json, write_json
from app.screening.filters import is_gas_token, is_stable, quote_allowed

LIST_PAGES = 10
NEW_PAGES = 2
TIMEFRAMES = (
    (Timeframe.M15, 2),
    (Timeframe.M5, 1),
    (Timeframe.M1, 1),
)


@dataclass
class Listed:
    chain: Chain
    address: str
    name: str
    liquidity: Decimal
    created: datetime | None
    source: str

    @property
    def age_hours(self) -> float:
        if self.created is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.created).total_seconds() / 3600


def _client(settings) -> JsonHttpClient:
    return JsonHttpClient(
        "geckoterminal", settings.geckoterminal_base_url,
        limiters={"default": RateLimiter(settings.geckoterminal_rpm, 60, burst=1)},
        rate_limit_penalty_s=30.0, timeout_s=settings.http_timeout_s, max_retries=settings.http_max_retries,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_s),
    )


def parse_listed(payload: dict, chain: Chain, source: str) -> list[Listed]:
    out: list[Listed] = []
    for item in (payload or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "pool"):
            continue
        attrs = item.get("attributes") or {}
        name = str(attrs.get("name") or "")
        raw_addr = str(attrs.get("address") or "")
        try:
            address = normalize_pool_id(chain, raw_addr)
        except Exception:
            continue
        liq_raw = attrs.get("reserve_in_usd")
        try:
            liquidity = Decimal(str(liq_raw)) if liq_raw not in (None, "") else Decimal(0)
        except Exception:
            liquidity = Decimal(0)
        created = None
        stamp = attrs.get("pool_created_at")
        if isinstance(stamp, str):
            try:
                created = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                created = None
        out.append(Listed(chain, address, name, liquidity, created, source))
    return out


def _quote_ok(chain: Chain, name: str) -> bool:
    """Name-based quote check used when the list payload has no token relationship."""
    upper = name.upper()
    if " / " not in upper:
        return False
    quote = upper.split(" / ", 1)[1]
    base = upper.split(" / ", 1)[0].strip()
    if base in {"SOL", "WETH", "ETH", "USDC", "USDT", "WBNB"}:
        return False
    hints = ("SOL", "USDC", "USDT", "WETH", "ETH")
    return any(token in quote for token in hints)


def stratify(rows: list[Listed], per_chain: int = 8) -> list[Listed]:
    """Older thin pools first, then liquid survivors, then a mid bucket to fill the quota."""
    chosen: list[Listed] = []
    seen: set[tuple[str, str]] = set()
    for chain in (Chain.SOLANA, Chain.BASE):
        pool_rows = []
        for row in rows:
            if row.chain is not chain or not _quote_ok(chain, row.name):
                continue
            key = (chain.value, row.address)
            if key in {(r.chain.value, r.address) for r in pool_rows}:
                continue
            pool_rows.append(row)
        thin = [r for r in pool_rows if r.liquidity < Decimal(8000) and r.age_hours >= 3]
        liquid = [r for r in pool_rows if r.liquidity >= Decimal(25000) and r.age_hours >= 24]
        mid = [r for r in pool_rows if Decimal(8000) <= r.liquidity < Decimal(25000) and r.age_hours >= 6]
        thin.sort(key=lambda r: r.created or datetime.max.replace(tzinfo=timezone.utc))
        liquid.sort(key=lambda r: r.liquidity, reverse=True)
        mid.sort(key=lambda r: r.age_hours, reverse=True)
        picked: list[Listed] = []
        for bucket, count in ((thin, 3), (liquid, 3), (mid, 2)):
            for row in bucket:
                key = (row.chain.value, row.address)
                if key in seen:
                    continue
                picked.append(row)
                seen.add(key)
                if sum(1 for r in picked if r in bucket) >= count:
                    break
        if len(picked) < per_chain:
            rest = sorted(pool_rows, key=lambda r: (r.age_hours < 3, r.liquidity))
            for row in rest:
                key = (row.chain.value, row.address)
                if key in seen:
                    continue
                picked.append(row)
                seen.add(key)
                if len(picked) >= per_chain:
                    break
        chosen.extend(picked[:per_chain])
    return chosen


def cohort_of(row: Listed) -> str:
    if row.liquidity < Decimal(8000):
        return "thin"
    if row.liquidity >= Decimal(25000):
        return "liquid"
    return "mid"


def _serialize_candles(candles) -> list:
    return [[c.open_time.isoformat(), format(c.open, "f"), format(c.high, "f"), format(c.low, "f"),
             format(c.close, "f"), format(c.volume_usd, "f"), c.is_closed] for c in candles]


async def _cached_get(client: JsonHttpClient, relative: str, path: str, params: dict | None) -> dict | None:
    hit = read_json(relative)
    if hit is not None:
        return hit
    try:
        payload = await client.get_json(path, params, cache_ttl_s=0)
    except ProviderError as exc:
        print(f"fetch failed {path} {params}: {exc}", flush=True)
        return None
    if isinstance(payload, dict):
        write_json(relative, payload)
    return payload if isinstance(payload, dict) else None


async def _ohlcv(provider: GeckoTerminalProvider, chain: Chain, address: str, timeframe: Timeframe, pages: int):
    merged = {}
    before: int | None = None
    for page in range(pages):
        relative = f"raw/{chain.value}/{address}/{timeframe.value}-{page}-{before or 'latest'}.json"
        cached = read_json(relative)
        if cached is None:
            try:
                candles = await provider.get_ohlcv(chain, address, timeframe, 1000, before)
            except ProviderError as exc:
                print(f"ohlcv failed {chain.value} {address} {timeframe.value}: {exc}", flush=True)
                break
            write_json(relative, _serialize_candles(candles))
        else:
            candles = _candles_from(cached, timeframe)
        if not candles:
            break
        for candle in candles:
            merged[candle.open_time] = candle
        oldest = min(c.open_time for c in candles)
        nxt = int(oldest.timestamp())
        if before is not None and nxt >= before:
            break
        before = nxt
    ordered = [merged[k] for k in sorted(merged)]
    write_json(f"candles/{chain.value}/{address}/{timeframe.value}.json", _serialize_candles(ordered))
    return ordered


def _candles_from(rows, timeframe: Timeframe):
    from app.domain.market import Candle
    out = []
    for row in rows:
        opened = datetime.fromisoformat(row[0])
        out.append(Candle(opened, timeframe, Decimal(row[1]), Decimal(row[2]), Decimal(row[3]),
                          Decimal(row[4]), Decimal(row[5]), bool(row[6])))
    return out


async def _detail(provider: GeckoTerminalProvider, chain: Chain, address: str, allow_major: bool = False):
    relative = f"pools/{chain.value}/{address}.json"
    cached = read_json(relative)
    if cached is not None:
        if allow_major and cached.get("skip") == "base":
            cached = dict(cached)
            cached.pop("skip", None)
            write_json(relative, cached)
        return cached
    try:
        snap = await provider.get_pool(chain, address)
    except ProviderError as exc:
        print(f"pool failed {chain.value} {address}: {exc}", flush=True)
        return None
    if snap is None:
        return None
    payload = {
        "chain": chain.value,
        "address": snap.pool_address,
        "token": snap.base_token.address,
        "symbol": snap.base_token.symbol,
        "quote": snap.quote_token.address if snap.quote_token else None,
        "liquidity_usd": format(snap.liquidity_usd, "f") if snap.liquidity_usd is not None else None,
        "fee_rate": format(fee_rate_for_snapshot(snap), "f"),
        "created": snap.pool_created_at.isoformat() if snap.pool_created_at else None,
        "name": (snap.raw.get("attributes") or {}).get("name") if isinstance(snap.raw, dict) else None,
    }
    # Reject pools we would not actually quote into a major.
    if snap.quote_token is None or not quote_allowed(chain, snap.quote_token.address):
        payload["skip"] = "quote"
    elif not allow_major and (is_stable(chain, snap.base_token.address) or is_gas_token(chain, snap.base_token.address)):
        payload["skip"] = "base"
    write_json(relative, payload)
    return payload


async def _benchmarks(provider: GeckoTerminalProvider, client: JsonHttpClient) -> list[dict]:
    found = []
    for chain, query, symbol in (
        (Chain.SOLANA, "SOL USDC", "SOL"),
        (Chain.BASE, "WETH USDC", "WETH"),
    ):
        relative = f"search/{chain.value}-{symbol}.json"
        payload = await _cached_get(client, relative, "/search/pools", {"query": query})
        best = None
        best_liq = Decimal(-1)
        network = chain.value
        for item in (payload or {}).get("data") or []:
            if not str(item.get("id") or "").startswith(f"{network}_"):
                continue
            attrs = item.get("attributes") or {}
            name = str(attrs.get("name") or "")
            if not name.upper().startswith(symbol + " /"):
                continue
            try:
                address = normalize_pool_id(chain, str(attrs.get("address") or ""))
            except Exception:
                continue
            try:
                liq = Decimal(str(attrs.get("reserve_in_usd") or "0"))
            except Exception:
                liq = Decimal(0)
            if liq > best_liq:
                best_liq = liq
                best = address
        if best:
            detail = await _detail(provider, chain, best, allow_major=True)
            if detail and detail.get("skip") != "quote":
                detail["cohort"] = "benchmark"
                detail["symbol"] = symbol
                write_json(f"pools/{chain.value}/{best}.json", detail)
                found.append(detail)
                print(f"benchmark {chain.value} {symbol} {best} liq={best_liq}", flush=True)
    return found


async def collect(per_chain: int = 8) -> dict:
    settings = get_settings()
    client = _client(settings)
    provider = GeckoTerminalProvider(client, [Chain.SOLANA, Chain.BASE])
    listed: list[Listed] = []
    try:
        for chain in (Chain.SOLANA, Chain.BASE):
            network = chain.value
            for source, path, pages in (
                ("pools", f"/networks/{network}/pools", LIST_PAGES),
                ("new", f"/networks/{network}/new_pools", NEW_PAGES),
            ):
                for page in range(1, pages + 1):
                    relative = f"lists/{network}-{source}-p{page}.json"
                    payload = await _cached_get(client, relative, path, {"page": page})
                    if not payload or not payload.get("data"):
                        break
                    listed.extend(parse_listed(payload, chain, source))
                    print(f"listed {network} {source} page {page}: {len(payload.get('data') or [])}", flush=True)
        picks = stratify(listed, per_chain=per_chain)
        print(f"selected {len(picks)} pools", flush=True)
        kept = []
        for row in picks:
            detail = await _detail(provider, row.chain, row.address)
            if not detail or detail.get("skip"):
                print(f"skip detail {row.chain.value} {row.name} {detail.get('skip') if detail else 'missing'}", flush=True)
                continue
            detail["cohort"] = cohort_of(row)
            detail["list_liquidity_usd"] = format(row.liquidity, "f")
            bars = {}
            launch = None
            for timeframe, pages in TIMEFRAMES:
                candles = await _ohlcv(provider, row.chain, row.address, timeframe, pages)
                bars[timeframe.value] = len([c for c in candles if c.is_closed])
                if timeframe is Timeframe.M15 and candles and row.created is not None:
                    oldest = min(c.open_time for c in candles)
                    launch = (oldest - row.created).total_seconds() < 3 * 3600
            detail["bars"] = bars
            detail["includes_launch"] = bool(launch)
            write_json(f"pools/{row.chain.value}/{row.address}.json", detail)
            kept.append(detail)
            print(f"kept {detail['cohort']} {row.chain.value} {detail.get('symbol')} bars={bars}", flush=True)
        benches = await _benchmarks(provider, client)
        for bench in benches:
            for timeframe, pages in ((Timeframe.M15, 2),):
                candles = await _ohlcv(provider, Chain(bench["chain"]), bench["address"], timeframe, pages)
                bench.setdefault("bars", {})[timeframe.value] = len(candles)
                write_json(f"pools/{bench['chain']}/{bench['address']}.json", bench)
        manifest = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "liquidity_note": "OHLCV has no reserve column. liquidity_usd is the spot snapshot at fetch time.",
            "listing_note": "new_pools pages are only the last few minutes. /pools is capped around page 10 on the keyless API (401 after that). Thin cohort is low spot liquidity and age >= 3h, not a historical new-pool archive.",
            "pools": kept,
            "benchmarks": benches,
        }
        write_json("manifest.json", manifest)
        print(f"manifest pools={len(kept)} benchmarks={len(benches)}", flush=True)
        return manifest
    finally:
        await provider.aclose()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Cache GeckoTerminal candles for the research sample. No trading.")
    parser.add_argument("--per-chain", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(collect(args.per_chain))


if __name__ == "__main__":
    main()
