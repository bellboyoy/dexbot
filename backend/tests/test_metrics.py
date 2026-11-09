from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from app.domain.market import DataSource, WindowStats
from app.intelligence.metrics import MetricsEngine
from app.intelligence.momentum import MomentumConfig, MomentumDetector, triggers
from app.quality.checks import QualityReport, check_snapshot
from tests.factories import NOW, snap

OK = QualityReport()


def with_windows(s, **w):
    windows = dict(s.windows)
    windows.update(w)
    return replace(s, windows=windows)


def test_volume_acceleration_and_buy_ratio():
    s = with_windows(snap(), m5=WindowStats(buys=30, sells=10, volume_usd=Decimal(3000)),
                     h1=WindowStats(buys=60, sells=40, volume_usd=Decimal(12000)),
                     h24=WindowStats(buys=900, sells=800, volume_usd=Decimal(96000)))
    m = MetricsEngine().compute(s, OK)
    assert m.volume_accel_m5 == Decimal(3)          # 3000 vs 12000/12
    assert m.volume_accel_h1 == Decimal(3)          # 12000 vs 96000/24
    assert m.buy_ratio_m5 == Decimal("0.75") and m.buy_ratio_h1 == Decimal("0.6")


def test_missing_inputs_give_none_not_zero():
    s = with_windows(snap(), m5=WindowStats(), h1=WindowStats(buys=0, sells=0, volume_usd=Decimal(0)))
    m = MetricsEngine().compute(s, OK)
    assert m.volume_accel_m5 is None and m.buy_ratio_m5 is None and m.buy_ratio_h1 is None
    assert m.buyers_h1 is None and m.liquidity_change_5m_pct is None


def test_liquidity_change_uses_own_history():
    eng = MetricsEngine()
    base = snap(liquidity="100000")
    eng.compute(base, OK)
    later = replace(snap(liquidity="120000"), observed_at=NOW + timedelta(minutes=5))
    assert eng.compute(later, OK).liquidity_change_5m_pct == Decimal(20)
    # No observation near 1h ago -> unknown
    assert eng.compute(later, OK).liquidity_change_1h_pct is None


def test_unreliable_observations_do_not_enter_history():
    eng = MetricsEngine()
    bad = QualityReport(flags=["stale"])
    eng.compute(snap(liquidity="1"), bad)
    later = replace(snap(liquidity="120000"), observed_at=NOW + timedelta(minutes=5))
    m = eng.compute(later, OK)
    assert m.liquidity_change_5m_pct is None


def test_buyers_only_used_when_fresh():
    eng = MetricsEngine()
    g = with_windows(snap(source=DataSource.GECKOTERMINAL), h1=WindowStats(buys=50, sells=20, buyers=31, sellers=12))
    eng.observe_buyers(g)
    assert eng.compute(snap(), OK).buyers_h1 == 31
    old = replace(snap(), observed_at=NOW + timedelta(minutes=30))
    assert eng.compute(old, OK).buyers_h1 is None      # 30-minute-old count is not substituted


def test_unreliable_metrics_never_trigger_momentum():
    s = with_windows(snap(), m5=WindowStats(buys=90, sells=10, volume_usd=Decimal(50000)),
                     h1=WindowStats(buys=300, sells=100, volume_usd=Decimal(60000)))
    m = MetricsEngine().compute(s, OK)
    assert "volume_spike" in triggers(m, "established", MomentumConfig())
    m.reliable = False
    assert triggers(m, "established", MomentumConfig()) == []


def test_momentum_cooldown():
    s = with_windows(snap(), m5=WindowStats(buys=90, sells=10, volume_usd=Decimal(50000)),
                     h1=WindowStats(buys=300, sells=100, volume_usd=Decimal(60000)))
    det = MomentumDetector()
    m = MetricsEngine().compute(s, check_snapshot(s, NOW))
    assert set(det.evaluate(m, "established")) >= {"volume_spike", "buy_pressure"}
    assert det.evaluate(m, "established") is None                   # within cooldown
    m.observed_at = NOW + timedelta(minutes=16)
    assert det.evaluate(m, "established") is not None
