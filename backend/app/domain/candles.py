"""Candle resampling. Higher timeframes are built from stored lower-timeframe candles so
multi-timeframe history costs no extra API calls.

Providers omit intervals with no trades, so a missing 1m candle means "no trades or no
data", not zero. A resampled bucket is emitted only if it lies entirely inside the span
the input actually covers; partial buckets at either edge are dropped rather than guessed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.domain.market import Candle, Timeframe


def resample(candles: list[Candle], target: Timeframe) -> list[Candle]:
    if not candles:
        return []
    src = candles[0].timeframe
    if any(c.timeframe is not src for c in candles):
        raise ValueError("mixed input timeframes")
    if target.seconds <= src.seconds or target.seconds % src.seconds:
        raise ValueError(f"cannot resample {src.value} into {target.value}")
    closed = sorted((c for c in candles if c.is_closed), key=lambda c: c.open_time)
    if not closed:
        return []
    span_start = int(closed[0].open_time.timestamp())
    span_end = int(closed[-1].close_time.timestamp())
    step = target.seconds
    buckets: dict[int, list[Candle]] = {}
    for c in closed:
        ts = int(c.open_time.timestamp())
        buckets.setdefault(ts - ts % step, []).append(c)
    out = []
    for start in sorted(buckets):
        if start < span_start or start + step > span_end:
            continue                        # bucket not fully covered by the input
        group = buckets[start]
        out.append(Candle(
            open_time=datetime.fromtimestamp(start, tz=timezone.utc), timeframe=target,
            open=group[0].open, high=max(c.high for c in group), low=min(c.low for c in group),
            close=group[-1].close, volume_usd=sum((c.volume_usd for c in group), start=group[0].volume_usd * 0),
            is_closed=True))
    return out
