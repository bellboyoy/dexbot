"""In-memory paper ledger. The database runner applies the same fill and P&L functions.

Equity = cash + deployed cost basis + unrealised P&L.
Unrealised P&L is the estimated exit proceeds, after fee and gas, minus cost basis.
Just after a fill that equity is below the starting balance: the exit is not free.
When nothing is open, equity equals cash and cash equals starting balance plus realised P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.costs import Fill, realized_pnl, unrealized_pnl
from app.domain.chains import Chain


@dataclass(slots=True)
class OpenPos:
    key: str
    chain: Chain
    pool_address: str
    token_address: str
    strategy: str
    qty: Decimal
    cost_basis: Decimal
    entry_exec: Decimal
    entry_mid: Decimal
    opened_at: datetime
    stop: Decimal
    take_profit: Decimal
    max_hold_until: datetime
    max_hold_bars: int | None
    liquidity: Decimal
    fee_rate: Decimal
    gas_per_tx: Decimal
    fees: Decimal
    gas: Decimal
    slippage: Decimal
    notional: Decimal
    bars_held: int = 0
    unrealized: Decimal = Decimal(0)
    mark: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    strategy: str
    chain: str
    pool_address: str
    net_pnl: Decimal
    fees: Decimal
    gas: Decimal
    slippage: Decimal
    reason: str
    opened_at: datetime
    closed_at: datetime
    entry_exec: Decimal
    exit_exec: Decimal
    notional: Decimal


@dataclass
class Ledger:
    starting: Decimal
    cash: Decimal | None = None
    realized: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    gas: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)
    positions: dict[str, OpenPos] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash is None:
            self.cash = self.starting

    def open_position(self, pos: OpenPos, fill: Fill) -> None:
        if fill.quote_spent is None:
            raise ValueError("open requires a buy fill")
        self.cash -= fill.quote_spent + fill.gas_usd
        self.fees += fill.fee_usd
        self.gas += fill.gas_usd
        self.slippage += fill.slippage_usd
        pos.unrealized = unrealized_pnl(
            cost_basis=pos.cost_basis, qty=pos.qty, mid=fill.mid, liquidity=pos.liquidity,
            fee_rate=pos.fee_rate, exit_gas=pos.gas_per_tx,
        )
        pos.mark = fill.mid
        self.positions[pos.key] = pos

    def mark(self, key: str, mid: Decimal) -> Decimal:
        pos = self.positions[key]
        pos.mark = mid
        pos.unrealized = unrealized_pnl(
            cost_basis=pos.cost_basis, qty=pos.qty, mid=mid, liquidity=pos.liquidity,
            fee_rate=pos.fee_rate, exit_gas=pos.gas_per_tx,
        )
        return pos.unrealized

    def close_position(self, key: str, fill: Fill, reason: str, when: datetime) -> ClosedTrade:
        pos = self.positions.pop(key)
        if fill.quote_received is None:
            raise ValueError("close requires a sell fill")
        self.cash += fill.quote_received - fill.gas_usd
        self.fees += fill.fee_usd
        self.gas += fill.gas_usd
        self.slippage += fill.slippage_usd
        pnl = realized_pnl(pos.cost_basis, fill)
        self.realized += pnl
        return ClosedTrade(
            strategy=pos.strategy, chain=pos.chain.value, pool_address=pos.pool_address, net_pnl=pnl,
            fees=pos.fees + fill.fee_usd, gas=pos.gas + fill.gas_usd, slippage=pos.slippage + fill.slippage_usd,
            reason=reason, opened_at=pos.opened_at, closed_at=when, entry_exec=pos.entry_exec,
            exit_exec=fill.exec_price, notional=pos.notional,
        )

    @property
    def deployed(self) -> Decimal:
        return sum((p.cost_basis for p in self.positions.values()), Decimal(0))

    @property
    def unrealized(self) -> Decimal:
        return sum((p.unrealized for p in self.positions.values()), Decimal(0))

    @property
    def net_pnl(self) -> Decimal:
        return self.realized + self.unrealized

    @property
    def equity(self) -> Decimal:
        return self.cash + self.deployed + self.unrealized
