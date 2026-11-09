"""Splits, the pre-registered grid, and the out-of-sample promotion rule."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtest.engine import PoolSeries, ratchet_stop, regime_allows
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.research.grid import build_grid, is_neighbor
from app.research.splits import split_pools, split_time, walk_forward_windows
from app.research.validate import promote, select_on_train
from app.strategy.registry import build_strategy

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _pool(name: str, n: int = 20, price: str = "1") -> PoolSeries:
    candles = []
    for i in range(n):
        px = Decimal(price)
        candles.append(Candle(START + timedelta(minutes=15 * i), Timeframe.M15, px, px, px, px, Decimal("100"), True))
    return PoolSeries(Chain.SOLANA, name, name, name, Decimal("50000"), Decimal("0.003"), candles)


def test_time_split_does_not_share_a_bar():
    pools = [_pool("a", 40), _pool("b", 40)]
    train, test, cut = split_time(pools, 0.7)
    assert cut is not None
    train_times = {c.open_time for p in train for c in p.candles}
    test_times = {c.open_time for p in test for c in p.candles}
    assert train_times and test_times
    assert train_times.isdisjoint(test_times)
    assert max(train_times) < min(test_times)


def test_pool_split_is_disjoint_and_deterministic():
    pools = [_pool(f"p{i:02d}") for i in range(10)]
    train, hold = split_pools(pools, 0.3)
    assert {p.key for p in train}.isdisjoint({p.key for p in hold})
    assert len(hold) == 3 and len(train) == 7
    again, _ = split_pools(list(reversed(pools)), 0.3)
    assert [p.key for p in train] == [p.key for p in again]


def test_walk_forward_windows_stay_inside_the_sample():
    pools = [_pool("a", 50)]
    windows = walk_forward_windows(pools)
    assert len(windows) == 3
    all_times = {c.open_time for p in pools for c in p.candles}
    for train, test in windows:
        train_times = {c.open_time for p in train for c in p.candles}
        test_times = {c.open_time for p in test for c in p.candles}
        assert train_times.isdisjoint(test_times)
        assert train_times | test_times <= all_times
        assert max(train_times) < min(test_times)


def test_grid_is_fixed_and_every_config_has_a_neighbour():
    grid = build_grid()
    assert len(grid) == 35
    assert len({cfg.label for cfg in grid}) == 35
    for cfg in grid:
        assert any(is_neighbor(cfg, other) for other in grid if other.label != cfg.label)


def test_selector_never_receives_holdout_pools():
    grid = [cfg for cfg in build_grid() if cfg.family == "momentum"]
    seen = []

    def evaluate(cfg, pools, benchmarks):
        seen.append({p.pool_address for p in pools})
        return {"label": cfg.label, "trades": 25, "expectancy": "1" if cfg.size == Decimal(10) else "-1",
                "net_pnl": "10", "max_drawdown_pct": "0.01", "passed_gate": True, "reason": "ok",
                "win_rate": "0.5", "avg_win": "2", "avg_loss": "-1", "risk_reward": "2",
                "profit_factor": "2", "total_costs": "1", "max_drawdown_usd": "5", "wins": 10, "losses": 10}

    champ, _ = select_on_train(grid, [_pool("train")], {}, evaluate)
    assert champ is not None and champ.label == "m_f8_s10"
    assert all(addresses == {"train"} for addresses in seen)


def test_promotion_rejects_a_train_only_winner_and_an_island():
    good = {"trades": 30, "expectancy": "0.5", "net_pnl": "15", "max_drawdown_pct": "0.05",
            "passed_gate": True, "reason": "ok"}
    bad = {**good, "expectancy": "-0.2", "net_pnl": "-4", "passed_gate": False, "reason": "not positive"}
    thin = {**good, "trades": 3, "passed_gate": False, "reason": "inconclusive"}
    ok, reason = promote(good, good, [good, good, thin], [good])
    assert ok, reason
    assert promote(bad, good, [good, good, good], [good])[0] is False
    assert promote(good, thin, [good, good, good], [good])[0] is False
    assert promote(good, good, [good, bad, thin], [])[0] is False


def test_trailing_stop_only_moves_up_and_regime_ignores_the_future():
    assert ratchet_stop(Decimal("1"), Decimal("1.2"), Decimal("0.1")) == Decimal("1.08")
    assert ratchet_stop(Decimal("1.2"), Decimal("1.0"), Decimal("0.1")) == Decimal("1.2")
    bars = []
    for i, px in enumerate(["1", "1", "1.2"]):
        price = Decimal(px)
        bars.append(Candle(START + timedelta(minutes=15 * i), Timeframe.M15, price, price, price, price, Decimal(1), True))
    # Pad to the EMA warmup with a flat history, then a last bar that is in the future relative to `when`.
    hist = []
    for i in range(25):
        price = Decimal("1")
        hist.append(Candle(START + timedelta(minutes=15 * i), Timeframe.M15, price, price, price, price, Decimal(1), True))
    future = Candle(START + timedelta(minutes=15 * 40), Timeframe.M15, Decimal("5"), Decimal("5"), Decimal("5"),
                    Decimal("5"), Decimal(1), True)
    when = hist[20].open_time
    assert regime_allows(hist + [future], when) is False or regime_allows(hist, when) == regime_allows(hist + [future], when)
    # The future bar is after `when`, so the decision matches the history without it.
    assert regime_allows(hist + [future], when) == regime_allows(hist, when)


def test_strategy_params_round_trip_without_changing_defaults():
    assert build_strategy("volume_spike").max_hold_bars == 6
    assert build_strategy("volume_spike", {"multiple": 4, "hold": 4}).max_hold_bars == 4
    assert build_strategy("pullback", {"dip": 0.08}).dip == 0.08
    assert build_strategy("momentum", {"use_regime": True}).use_regime is True
