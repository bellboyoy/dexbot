"""Replay strategy validation on point-in-time launch snapshots.

The collector has to run for about two weeks before this will score anything. Until then
it prints an inventory and leaves the paper-trading gate alone. Liquidity for a candle is
the last snapshot at or before that candle. Later snapshots are not visible to earlier bars.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.backtest.engine import PoolSeries
from app.collector.schedule import asof_liquidity, daily_megabytes, snapshot_ages
from app.collector.store import Inventory, inventory, ready
from app.config import get_settings
from app.db.models import LaunchSnapshot, LaunchWatch
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.paper.repository import session_scope
from app.research.cache import read_json
from app.research.grid import build_grid, is_neighbor
from app.research.splits import split_pools, split_time, walk_forward_windows
from app.research.validate import _run, promote, select_on_train
from app.strategy.validation import dump_validation

MIN_CANDLE_POOLS = 8


def clip_candles(candles: list[Candle], points: list[tuple[datetime, Decimal | None]]) -> tuple[list[Candle], tuple[Decimal, ...]] | None:
    """Drop bars from before the first liquidity print, then attach an as-of reserve to each bar."""
    known = [(when, value) for when, value in points if value is not None and when is not None]
    if not known:
        return None
    first = min(when for when, _value in known)
    kept = [candle for candle in candles if candle.open_time >= first and candle.is_closed]
    if len(kept) < 40:
        return None
    series = []
    for candle in kept:
        value = asof_liquidity(known, candle.open_time)
        if value is None:
            return None
        series.append(value)
    return kept, tuple(series)


def _cached_candles(chain: str, address: str) -> list[Candle]:
    rows = read_json(f"candles/{chain}/{address}/15m.json") or []
    out = []
    for row in rows:
        out.append(Candle(
            datetime.fromisoformat(row[0]), Timeframe.M15, Decimal(row[1]), Decimal(row[2]), Decimal(row[3]),
            Decimal(row[4]), Decimal(row[5]), bool(row[6]),
        ))
    return out


async def load_pit_pools(session) -> list[PoolSeries]:
    watches = list(await session.scalars(select(LaunchWatch)))
    pools: list[PoolSeries] = []
    for watch in watches:
        snaps = list(await session.scalars(
            select(LaunchSnapshot)
            .where(LaunchSnapshot.watch_id == watch.id, LaunchSnapshot.missed.is_(False))
            .order_by(LaunchSnapshot.observed_at)
        ))
        points = [(snap.observed_at, snap.liquidity_usd) for snap in snaps if snap.observed_at is not None]
        clipped = clip_candles(_cached_candles(watch.chain.value, watch.pool_address), points)
        if clipped is None:
            continue
        candles, series = clipped
        last = series[-1]
        pools.append(PoolSeries(
            chain=watch.chain if isinstance(watch.chain, Chain) else Chain(watch.chain),
            pool_address=watch.pool_address, token_address=watch.token_address, symbol=watch.symbol,
            liquidity_usd=last, fee_rate=Decimal("0.003"), candles=candles, liquidity_series=series,
            liquidity_source="point_in_time", pool_created_at=watch.pool_created_at,
        ))
    return pools


def _print_inventory(stats: Inventory, reason: str) -> None:
    per_day = daily_megabytes(150)
    print("point-in-time inventory")
    print(f"  watches: {stats.watches}")
    coverage = "n/a" if stats.observed_coverage is None else f"{stats.observed_coverage:.0%}"
    print(f"  snapshots: {stats.snapshots} ({stats.real_snapshots} with a price or not-found, "
          f"{stats.unsupported_slots} unsupported pool ids, the rest missed)")
    print(f"  observed coverage: {coverage} ({stats.observed_slots} of {stats.elapsed_slots} elapsed slots)")
    print(f"  oldest: {stats.oldest.isoformat() if stats.oldest else 'none'}")
    print(f"  days covered: {stats.days:.2f}")
    print(f"  usable pools (8 observed slots and at least half of elapsed slots): {stats.pools_with_history}")
    print(f"  scheduled snapshots per pool: {len(snapshot_ages())}")
    print(f"  not ready: {reason}")
    print(f"  at the default cap of 150 new pools/day the table is about {per_day:.1f} MB/day plus indexes")


async def run(promote_ok: bool = False) -> dict:
    settings = get_settings()
    async with session_scope(settings.database_url) as session:
        stats = await inventory(session)
        ok, reason = ready(stats)
        if not ok:
            _print_inventory(stats, reason)
            return {"ready": False, "reason": reason}
        pools = await load_pit_pools(session)
    if len(pools) < MIN_CANDLE_POOLS:
        print(f"snapshots are old enough, but only {len(pools)} pools have cached 15-minute candles and a liquidity path")
        print("validation was not run and the paper gate was not changed")
        return {"ready": True, "pools": len(pools), "reason": "not enough candles"}
    grid = build_grid()
    families: dict[str, list] = {}
    for cfg in grid:
        families.setdefault(cfg.family, []).append(cfg)
    train_pools, holdout = split_pools(pools)
    train, time_oos, _cut = split_time(train_pools)
    passed: list[str] = []
    configs: dict[str, dict] = {}
    rejected: dict[str, str] = {}
    for family, family_configs in families.items():
        champ, rows = select_on_train(family_configs, train, {})
        if champ is None:
            rejected[family] = "no configuration had enough train trades"
            continue
        train_row = next(row for row in rows if row["label"] == champ.label)
        if Decimal(train_row["expectancy"] or "0") <= 0:
            rejected[family] = "train expectancy is not positive"
            print(f"{family} train net {train_row['net_pnl']} (not promoted)")
            continue
        time_row = _run(champ, time_oos, {})
        pool_row = _run(champ, holdout, {})
        folds = [_run(champ, fold_test, {}) for _fold_train, fold_test in walk_forward_windows(train)]
        neighbor_rows = [_run(other, time_oos, {}) for other in family_configs if is_neighbor(champ, other)]
        ok, why = promote(time_row, pool_row, folds, neighbor_rows)
        print(f"{family} {'PASS' if ok else 'FAIL'}: {why}")
        if ok:
            passed.append(family)
            configs[family] = champ.params
        else:
            rejected[family] = why
    print("passed:", ", ".join(passed) if passed else "(none)")
    if promote_ok and passed:
        dump_validation({
            "passed": passed,
            "rejected": rejected,
            "configs": configs,
            "timeframe": "15m",
            "note": "Promoted from point-in-time launch snapshots. Paper only.",
        })
        print("updated strategy_validation.json")
    else:
        print("strategy_validation.json was left unchanged")
    return {"ready": True, "passed": passed}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Rerun strategy validation on collected launch snapshots. Does not trade.")
    parser.add_argument("--promote", action="store_true", help="reserved; validation still has to pass before any paper strategy is enabled")
    args = parser.parse_args(argv)
    asyncio.run(run(args.promote))


if __name__ == "__main__":
    main()
