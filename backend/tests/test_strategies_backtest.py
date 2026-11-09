"""Strategies are deterministic on a closed prefix, and the backtest charges costs."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.backtest.engine import PoolSeries, run_backtest
from app.backtest.metrics import report
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.paper.runner import choose_exit
from app.strategy.base import Action, Signal
from app.strategy.breakout import Breakout
from app.strategy.mean_reversion import MeanReversion
from app.strategy.momentum import Momentum
from app.strategy.validation import MIN_TRADES, gate
from app.strategy.volume_spike import VolumeSpike

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles(rows: list[tuple[str, str, str]]) -> list[Candle]:
    out = []
    for i, (open_, close, volume) in enumerate(rows):
        o, c = Decimal(open_), Decimal(close)
        high, low = max(o, c), min(o, c)
        out.append(Candle(START + timedelta(minutes=5 * i), Timeframe.M5, o, high, low, c, Decimal(volume), True))
    return out


def test_volume_spike_and_breakout_fire_on_a_clear_bar():
    quiet = [("1", "1", "100")] * 22
    spike = quiet + [("1", "1.2", "500")]
    assert VolumeSpike().on_bar(candles(spike)).action is Action.ENTER
    assert VolumeSpike().on_bar(candles(quiet + [("1", "1.2", "100")])).action is Action.HOLD

    channel = [("1", "1", "100")] * 22
    assert Breakout().on_bar(candles(channel + [("1", "1.4", "200")])).action is Action.ENTER
    assert Breakout().on_bar(candles(channel + [("1", "0.5", "200")])).action is Action.EXIT


def test_mean_reversion_buys_a_break_of_a_flat_market():
    flat = [("100", "100", "100")] * 40
    crashed = flat + [("100", "90", "100")]
    signal = MeanReversion().on_bar(candles(crashed))
    assert signal.action is Action.ENTER


def test_signal_uses_only_the_prefix_it_is_given():
    series = candles([("100", str(100 + i * 0.2), "100") for i in range(80)])
    strategy = Momentum()
    for i in range(30, len(series)):
        first = strategy.on_bar(series[:i])
        second = strategy.on_bar(series[:i])
        assert first == second
        # A wild bar after the prefix must not change the decision at i.
        assert strategy.on_bar(series[:i]) == first


def test_future_bars_are_not_visible_to_the_strategy():
    class Recorder:
        name = "recorder"
        version = "1"
        max_hold_bars = None

        def __init__(self):
            self.lengths: list[int] = []

        def on_bar(self, bars):
            assert all(bar.is_closed for bar in bars)
            assert bars[-1].open_time == max(bar.open_time for bar in bars)
            self.lengths.append(len(bars))
            return Signal(self.name, Action.HOLD, "")

    pool = PoolSeries(Chain.BASE, "0x" + "ab" * 20, "0x" + "cd" * 20, "T", Decimal("100000"),
                      Decimal("0.003"), candles([("1", "1", "100")] * 12))
    recorder = Recorder()
    run_backtest(recorder, [pool])
    assert recorder.lengths == list(range(1, 13))


class _EnterOnce:
    name = "enter_once"
    version = "1"
    max_hold_bars = None
    fired = False

    def on_bar(self, bars):
        if len(bars) == 5 and not self.fired:
            self.fired = True
            return Signal(self.name, Action.ENTER, "now")
        return Signal(self.name, Action.HOLD, "")


def test_stop_fills_before_target_and_costs_are_charged():
    rows = [("1", "1", "1000")] * 5 + [("1", "1", "1000"), ("1", "1", "1000")]
    # Index 5 is the fill bar. Its range trades through both the stop and the target.
    series = candles(rows)
    series[5] = Candle(series[5].open_time, Timeframe.M5, Decimal("1"), Decimal("2"), Decimal("0.5"),
                       Decimal("1"), Decimal("1000"), True)
    result = run_backtest(_EnterOnce(), [_replace(series)])
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "stop_loss"
    assert trade.net_pnl < 0
    assert trade.fees > 0 and trade.gas > 0 and trade.slippage > 0
    summary = report("enter_once", result.trades, result.equity, Decimal("1000"))
    assert summary.net_pnl == trade.net_pnl
    assert summary.total_costs == trade.fees + trade.gas + trade.slippage


def test_take_profit_and_time_stop():
    class _Hold:
        name = "hold"
        version = "1"
        max_hold_bars = 2

        def on_bar(self, bars):
            if len(bars) == 4:
                return Signal(self.name, Action.ENTER, "now")
            return Signal(self.name, Action.HOLD, "")

    rising = candles([("1", "1", "1000")] * 4 + [("1", "1.5", "1000")] + [("1.5", "1.5", "1000")] * 4)
    # Fill at bar index 4 open, which is 1. The bar's high is 1.5, above a 15% target.
    result = run_backtest(_Hold(), [_replace(rising)])
    assert result.trades and result.trades[0].reason == "take_profit"

    flat = candles([("1", "1", "1000")] * 10)
    held = run_backtest(_Hold(), [_replace(flat)])
    assert held.trades and held.trades[0].reason == "time_stop"


def _replace(bars) -> PoolSeries:
    return PoolSeries(Chain.BASE, "0x" + "ab" * 20, "0x" + "cd" * 20, "T", Decimal("100000"),
                      Decimal("0.003"), bars)


def test_gate_rejects_thin_or_negative_samples():
    assert gate(5, Decimal("10"), Decimal("2"), Decimal("2"), Decimal("0.01")).passed is False
    assert "inconclusive" in gate(5, Decimal("10"), Decimal("2"), Decimal("2"), Decimal("0.01")).reason
    assert gate(MIN_TRADES, Decimal("-1"), Decimal("-0.05"), Decimal("0.5"), Decimal("0.01")).passed is False
    assert gate(MIN_TRADES, Decimal("10"), Decimal("0.5"), Decimal("1.2"), Decimal("0.4")).passed is False
    assert gate(MIN_TRADES, Decimal("10"), Decimal("0.5"), Decimal("1.2"), Decimal("0.1")).passed is True


def test_exit_priority_on_a_scan():
    now = START
    assert choose_exit(Decimal("0.9"), Decimal("0.92"), Decimal("1.2"), now, now + timedelta(hours=1)) == "stop_loss"
    assert choose_exit(Decimal("1.3"), Decimal("0.92"), Decimal("1.2"), now, now + timedelta(hours=1)) == "take_profit"
    assert choose_exit(Decimal("1"), Decimal("0.92"), Decimal("1.2"), now + timedelta(hours=2), now) == "time_stop"
    assert choose_exit(Decimal("1"), Decimal("0.92"), Decimal("1.2"), now, now + timedelta(hours=1)) is None
