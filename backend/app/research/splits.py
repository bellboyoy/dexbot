"""Train/test cuts. Parameter search is only allowed to see the train side."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from app.backtest.engine import PoolSeries


def split_pools(pools: list[PoolSeries], holdout_frac: float = 0.3) -> tuple[list[PoolSeries], list[PoolSeries]]:
    """Hold out a deterministic slice of pools, by address, so the cut is not a performance pick."""
    ordered = sorted(pools, key=lambda pool: pool.key)
    if len(ordered) < 2:
        return ordered, []
    n_hold = max(1, int(round(len(ordered) * holdout_frac)))
    n_hold = min(n_hold, len(ordered) - 1)
    return ordered[:-n_hold], ordered[-n_hold:]


def _clip(pool: PoolSeries, start: datetime | None, end: datetime | None) -> PoolSeries:
    candles = [
        c for c in pool.candles
        if (start is None or c.open_time >= start) and (end is None or c.open_time < end)
    ]
    series = None
    if pool.liquidity_series is not None and len(pool.liquidity_series) == len(pool.candles):
        series = tuple(
            liq for c, liq in zip(pool.candles, pool.liquidity_series)
            if (start is None or c.open_time >= start) and (end is None or c.open_time < end)
        )
    return replace(pool, candles=candles, liquidity_series=series)


def time_cut(pools: list[PoolSeries], train_frac: float = 0.7) -> datetime | None:
    times = sorted(c.open_time for pool in pools for c in pool.candles)
    if not times:
        return None
    index = min(len(times) - 1, max(0, int((len(times) - 1) * train_frac)))
    return times[index]


def split_time(pools: list[PoolSeries], train_frac: float = 0.7) -> tuple[list[PoolSeries], list[PoolSeries], datetime | None]:
    """Same pools, earlier bars for train and later bars for test. No bar is in both."""
    cut = time_cut(pools, train_frac)
    if cut is None:
        return [], [], None
    train = [_clip(pool, None, cut) for pool in pools]
    test = [_clip(pool, cut, None) for pool in pools]
    return [p for p in train if len(p.candles) >= 2], [p for p in test if len(p.candles) >= 2], cut


def walk_forward_windows(pools: list[PoolSeries]) -> list[tuple[list[PoolSeries], list[PoolSeries]]]:
    """Three expanding-train windows on a pool set that is already the train universe.

    Test slices are [40%, 60%), [60%, 80%) and [80%, 100%] of this set's timestamps.
    """
    times = sorted(c.open_time for pool in pools for c in pool.candles)
    if len(times) < 20:
        return []
    indexes = [int((len(times) - 1) * frac) for frac in (0.4, 0.6, 0.8, 1.0)]
    cuts = [times[i] for i in indexes]
    windows = []
    for left, right, last in zip(cuts, cuts[1:], (False, False, True)):
        train = [_clip(pool, None, left) for pool in pools]
        end = None if last else right
        test = [_clip(pool, left, end) for pool in pools]
        train = [p for p in train if len(p.candles) >= 2]
        test = [p for p in test if len(p.candles) >= 2]
        if train and test:
            windows.append((train, test))
    return windows
