"""Bar-by-bar backtest. Signals see the closed prefix; fills happen at the next bar's open.

A stop or target is a resting order, so it can fill inside the bar that touches it.
If both are touched, the stop fills. A gap through the stop fills at the open, then slippage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from app.costs import buy_fill, gas_usd, sell_fill
from app.domain.chains import Chain
from app.domain.market import Candle, Timeframe
from app.paper.ledger import ClosedTrade, Ledger, OpenPos
from app.risk.engine import AccountSnapshot, EntryRequest, OpenExposure, RiskEngine
from app.risk.limits import RiskLimits
from app.strategy.base import Action, Strategy
from app.strategy.indicators import ema as _ema


@dataclass(frozen=True, slots=True)
class PoolSeries:
    chain: Chain
    pool_address: str
    token_address: str
    symbol: str | None
    liquidity_usd: Decimal
    fee_rate: Decimal
    candles: list[Candle]
    # Populated only when a real reserve history exists. Spot liquidity is the default,
    # and a haircut series used as a sensitivity check must say so in `liquidity_source`.
    liquidity_series: tuple[Decimal, ...] | None = None
    liquidity_source: str = "spot_snapshot"
    cohort: str = ""
    pool_created_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.pool_address}"


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    trades: list[ClosedTrade]
    equity: list[Decimal]
    bars: int


def _closed_only(pool: PoolSeries) -> PoolSeries:
    candles = [c for c in pool.candles if c.is_closed and min(c.open, c.high, c.low, c.close) > 0]
    return replace(pool, candles=candles)


def liquidity_at(pool: PoolSeries, index: int) -> Decimal:
    series = pool.liquidity_series
    if series is not None and 0 <= index < len(series) and series[index] > 0:
        return series[index]
    return pool.liquidity_usd


def regime_allows(benchmark: list[Candle], when: datetime) -> bool:
    """True when the benchmark's last closed bar at or before `when` is above its EMA20.

    Bars after `when` are ignored, so the filter does not look ahead.
    """
    hist = [c for c in benchmark if c.is_closed and c.open_time <= when and c.close > 0]
    if len(hist) < 21:
        return False
    closes = [float(c.close) for c in hist]
    slow = _ema(closes, 20)
    return slow[-1] is not None and closes[-1] > slow[-1]


def ratchet_stop(stop: Decimal, high: Decimal, trail_pct: float | Decimal) -> Decimal:
    """Long trailing stop. Only moves up."""
    candidate = high * (Decimal(1) - Decimal(str(trail_pct)))
    return candidate if candidate > stop else stop


def _intrabar(pos: OpenPos, candle: Candle) -> tuple[str, Decimal] | None:
    stop_hit = candle.low <= pos.stop
    tp_hit = candle.high >= pos.take_profit
    if stop_hit:
        mid = candle.open if candle.open < pos.stop else pos.stop
        return "stop_loss", mid
    if tp_hit:
        mid = candle.open if candle.open > pos.take_profit else pos.take_profit
        return "take_profit", mid
    return None


def _snapshot(ledger: Ledger, state: dict, now: datetime, limits: RiskLimits) -> AccountSnapshot:
    day = now.astimezone(timezone.utc).date()
    if state["day"] != day:
        state["day"] = day
        state["day_pnl"] = Decimal(0)
        state["kill"] = False
    return AccountSnapshot(
        cash_usd=ledger.cash,
        day_realized_pnl_usd=state["day_pnl"],
        consecutive_losses=state["losses"],
        cooldown_until=state["cooldown"],
        kill_switch=state["kill"],
        open_positions=[
            OpenExposure(p.chain, p.pool_address, p.token_address, p.cost_basis) for p in ledger.positions.values()
        ],
    )


def _remember(state: dict, snap: AccountSnapshot) -> None:
    state["day_pnl"] = snap.day_realized_pnl_usd
    state["losses"] = snap.consecutive_losses
    state["cooldown"] = snap.cooldown_until
    state["kill"] = snap.kill_switch


def run_backtest(strategy: Strategy, pools: list[PoolSeries], *, starting: Decimal = Decimal("1000"),
                 limits: RiskLimits | None = None,
                 benchmarks: dict[Chain, list[Candle]] | None = None) -> BacktestResult:
    limits = limits or RiskLimits()
    risk = RiskEngine(limits)
    pools = [_closed_only(pool) for pool in pools]
    pools = [pool for pool in pools if len(pool.candles) >= 2]
    ledger = Ledger(starting)
    trades: list[ClosedTrade] = []
    equity: list[Decimal] = [starting]
    pending_entry: dict[str, PoolSeries] = {}
    pending_exit: dict[str, str] = {}
    by_key = {p.key: p for p in pools}
    state = {"day": None, "day_pnl": Decimal(0), "losses": 0, "cooldown": None, "kill": False}

    events: list[tuple[datetime, str, int]] = []
    for pool in pools:
        for i, candle in enumerate(pool.candles):
            if not candle.is_closed or candle.close <= 0 or candle.open <= 0:
                continue
            events.append((candle.open_time, pool.key, i))
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    for open_time, key, index in events:
        pool = by_key[key]
        candle = pool.candles[index]
        gas = gas_usd(pool.chain)
        if key in pending_exit and key in ledger.positions:
            reason = pending_exit.pop(key)
            _close(ledger, trades, risk, state, key, candle.open, candle.open_time, pool, gas, reason, index)
        pending_exit.pop(key, None)
        if key in pending_entry and key not in ledger.positions:
            pending_entry.pop(key)
            _try_open(ledger, risk, state, limits, strategy, pool, candle.open, candle.open_time, gas, index)
        hit = _intrabar(ledger.positions[key], candle) if key in ledger.positions else None
        if hit is not None:
            pending_exit.pop(key, None)
            _close(ledger, trades, risk, state, key, hit[1], candle.open_time, pool, gas, hit[0], index)
        trail = getattr(strategy, "trail_pct", None)
        if trail and key in ledger.positions:
            pos = ledger.positions[key]
            pos.stop = ratchet_stop(pos.stop, candle.high, trail)
        if key in ledger.positions:
            ledger.mark(key, candle.close)
            pos = ledger.positions[key]
            pos.bars_held += 1
            hold_cap = strategy.max_hold_bars
            timed = (hold_cap is not None and pos.bars_held >= hold_cap) or candle.close_time >= pos.max_hold_until
            if timed:
                pending_exit[key] = "time_stop"
            else:
                signal = strategy.on_bar(pool.candles[:index + 1])
                if signal.action is Action.EXIT:
                    pending_exit[key] = "signal"
        else:
            signal = strategy.on_bar(pool.candles[:index + 1])
            if signal.action is Action.ENTER and key not in pending_entry and _regime_ok(strategy, pool, candle, benchmarks):
                pending_entry[key] = pool
        equity.append(ledger.equity)

    for key, pos in list(ledger.positions.items()):
        pool = by_key[key]
        last = pool.candles[-1]
        gas = gas_usd(pool.chain)
        _close(ledger, trades, risk, state, key, last.close, last.close_time, pool, gas, "end_of_data",
               len(pool.candles) - 1)
        equity.append(ledger.equity)
    return BacktestResult(strategy.name, trades, equity, len(events))


def _regime_ok(strategy, pool: PoolSeries, candle: Candle, benchmarks: dict[Chain, list[Candle]] | None) -> bool:
    if not getattr(strategy, "use_regime", False):
        return True
    bench = (benchmarks or {}).get(pool.chain)
    if not bench:
        return False
    return regime_allows(bench, candle.open_time)


def _try_open(ledger, risk, state, limits, strategy, pool: PoolSeries, mid: Decimal, when: datetime,
              gas: Decimal, index: int) -> None:
    liquidity = liquidity_at(pool, index)
    snap = _snapshot(ledger, state, when, limits)
    decision = risk.evaluate_entry(snap, EntryRequest(
        chain=pool.chain, pool_address=pool.pool_address, token_address=pool.token_address,
        strategy=strategy.name, mid_price=mid, liquidity_usd=liquidity, fee_rate=pool.fee_rate, now=when,
    ))
    _remember(state, snap)
    if not decision.approved or decision.size_usd is None:
        return
    fill = buy_fill(mid=mid, notional=decision.size_usd, liquidity=liquidity,
                    fee_rate=pool.fee_rate, gas_usd=gas)
    stop = fill.exec_price * (Decimal(1) - decision.stop_loss_pct)
    take = fill.exec_price * (Decimal(1) + decision.take_profit_pct)
    ledger.open_position(OpenPos(
        key=pool.key, chain=pool.chain, pool_address=pool.pool_address, token_address=pool.token_address,
        strategy=strategy.name, qty=fill.qty, cost_basis=fill.quote_spent + fill.gas_usd,
        entry_exec=fill.exec_price, entry_mid=mid, opened_at=when, stop=stop, take_profit=take,
        max_hold_until=decision.max_hold_until, max_hold_bars=strategy.max_hold_bars,
        liquidity=liquidity, fee_rate=pool.fee_rate, gas_per_tx=gas, fees=fill.fee_usd,
        gas=fill.gas_usd, slippage=fill.slippage_usd, notional=fill.quote_spent,
    ), fill)


def _close(ledger, trades, risk, state, key, mid, when, pool: PoolSeries, gas, reason: str, index: int) -> None:
    pos = ledger.positions[key]
    fill = sell_fill(qty=pos.qty, mid=mid, liquidity=liquidity_at(pool, index), fee_rate=pool.fee_rate, gas_usd=gas)
    trade = ledger.close_position(key, fill, reason, when)
    trades.append(trade)
    snap = _snapshot(ledger, state, when, RiskLimits())
    risk.register_close(trade.net_pnl, when, snap)
    _remember(state, snap)


def timeframe_of(pools: list[PoolSeries]) -> Timeframe:
    for pool in pools:
        if pool.candles:
            return pool.candles[0].timeframe
    return Timeframe.M5
