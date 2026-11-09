"""Performance numbers for one strategy. Money stays in Decimal."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.paper.ledger import ClosedTrade
from app.strategy.validation import gate, risk_adjusted_score


@dataclass(frozen=True, slots=True)
class StrategyReport:
    strategy: str
    trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    risk_reward: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal
    fees: Decimal
    gas: Decimal
    slippage: Decimal
    total_costs: Decimal
    net_pnl: Decimal
    ending_equity: Decimal
    end_of_data_exits: int
    passed: bool
    reason: str
    score: Decimal


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def max_drawdown(equity: list[Decimal]) -> Decimal:
    peak = equity[0] if equity else Decimal(0)
    worst = Decimal(0)
    for point in equity:
        if point > peak:
            peak = point
        drop = peak - point
        if drop > worst:
            worst = drop
    return worst


def report(strategy: str, trades: list[ClosedTrade], equity: list[Decimal], starting: Decimal) -> StrategyReport:
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl < 0]
    flat = [t for t in trades if t.net_pnl == 0]
    avg_win = _avg(wins)
    avg_loss = _avg(losses)
    reward = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        reward = avg_win / abs(avg_loss)
    gross_win = sum(wins, Decimal(0))
    gross_loss = sum(losses, Decimal(0))
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else None
    net = sum((t.net_pnl for t in trades), Decimal(0))
    expectancy = (net / Decimal(len(trades))) if trades else None
    fees = sum((t.fees for t in trades), Decimal(0))
    gas = sum((t.gas for t in trades), Decimal(0))
    slip = sum((t.slippage for t in trades), Decimal(0))
    dd = max_drawdown(equity or [starting])
    dd_pct = (dd / starting) if starting > 0 else Decimal(0)
    decision = gate(len(trades), net, expectancy, profit_factor, dd_pct)
    ending = equity[-1] if equity else starting
    return StrategyReport(
        strategy=strategy, trades=len(trades), wins=len(wins), losses=len(losses), breakeven=len(flat),
        win_rate=(Decimal(len(wins)) / Decimal(len(trades))) if trades else None,
        avg_win=avg_win, avg_loss=avg_loss, risk_reward=reward, profit_factor=profit_factor,
        expectancy=expectancy, max_drawdown_usd=dd, max_drawdown_pct=dd_pct, fees=fees, gas=gas,
        slippage=slip, total_costs=fees + gas + slip, net_pnl=net, ending_equity=ending,
        end_of_data_exits=sum(1 for t in trades if t.reason == "end_of_data"),
        passed=decision.passed, reason=decision.reason, score=risk_adjusted_score(net, dd),
    )
