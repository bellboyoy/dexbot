from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from app.domain.market import Candle, DataSource, Timeframe, WindowStats
from app.quality.checks import check_candles, check_divergence, check_snapshot
from tests.factories import NOW, snap


def full(**kw):
    s = snap(**kw)
    w = dict(s.windows)
    w["m5"] = WindowStats(buys=5, sells=4, volume_usd=Decimal(900))
    w["h1"] = replace(w["h1"], volume_usd=Decimal(10000))
    return replace(s, windows=w)


def test_clean_snapshot_has_no_flags():
    r = check_snapshot(full(), NOW)
    assert r.flags == [] and r.reliable


def test_stale_fetch_is_flagged_and_unreliable():
    r = check_snapshot(full(), NOW + timedelta(minutes=5))
    assert "stale" in r.flags and not r.reliable and r.details["stale"]["age_s"] == 300


def test_missing_is_flagged_not_zero():
    r = check_snapshot(full(liquidity=None, h1=None), NOW)
    assert {"missing_liquidity", "missing_activity"} <= set(r.flags) and not r.reliable


def test_frozen_provider_data_detected():
    a = full()
    b = replace(a, observed_at=NOW + timedelta(minutes=4))
    r = check_snapshot(b, NOW + timedelta(minutes=4), previous=a)
    assert "frozen" in r.flags and not r.reliable
    # Short gaps are normal (counters can legitimately repeat within a minute).
    assert "frozen" not in check_snapshot(replace(a, observed_at=NOW + timedelta(seconds=30)),
                                          NOW + timedelta(seconds=30), previous=a).flags


def test_liquidity_jump_is_warning_not_blocking():
    a = full()
    b = replace(full(liquidity="10000"), observed_at=NOW + timedelta(minutes=1))
    r = check_snapshot(b, NOW + timedelta(minutes=1), previous=a)
    assert "liquidity_jump" in r.flags and r.reliable


def test_negative_values_invalid():
    assert "invalid_values" in check_snapshot(full(liquidity="-5"), NOW).flags


def test_cross_source_divergence():
    a = full()
    b = replace(a, source=DataSource.GECKOTERMINAL, price_usd=a.price_usd * Decimal("1.2"))
    r = check_divergence(a, b)
    assert r.flags == ["source_divergence_price"]
    assert check_divergence(a, replace(b, price_usd=None)).flags == []      # unknown is not divergence


def _candles(minutes):
    return [Candle(NOW + timedelta(minutes=m), Timeframe.M1, *(Decimal(1),) * 4, Decimal(5), True) for m in minutes]


def test_candle_gaps_and_lag():
    r = check_candles(_candles([0, 1, 4, 5]), NOW + timedelta(minutes=7))
    assert r.details["candle_gaps"] == {"missing": 2, "of": 6} and "candles_lagging" not in r.flags
    assert "candles_lagging" in check_candles(_candles([0, 1]), NOW + timedelta(hours=1)).flags
    assert check_candles([], NOW).flags == ["no_candles"]
