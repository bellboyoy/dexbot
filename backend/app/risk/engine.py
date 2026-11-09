"""The only path that can approve a new paper position.

Size is the configured maximum, every time. It does not grow after a loss.
A second buy in a token or pool that is already open is refused (no averaging down).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.costs import CostModelError, gas_usd, price_impact, round_trip_cost
from app.domain.chains import Chain
from app.risk.limits import RiskLimits


@dataclass(slots=True)
class OpenExposure:
    chain: Chain
    pool_address: str
    token_address: str
    cost_basis_usd: Decimal


@dataclass(slots=True)
class AccountSnapshot:
    cash_usd: Decimal
    day_realized_pnl_usd: Decimal
    consecutive_losses: int
    cooldown_until: datetime | None
    kill_switch: bool
    open_positions: list[OpenExposure] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EntryRequest:
    chain: Chain
    pool_address: str
    token_address: str
    strategy: str
    mid_price: Decimal
    liquidity_usd: Decimal
    fee_rate: Decimal
    now: datetime


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    size_usd: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    max_hold_until: datetime | None = None
    planned_max_loss_usd: Decimal | None = None


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def order_size(self) -> Decimal:
        """Fixed dollar size. The loss streak is intentionally not an argument."""
        return self.limits.max_position_usd

    def evaluate_entry(self, account: AccountSnapshot, req: EntryRequest) -> RiskDecision:
        limits = self.limits
        if account.kill_switch or account.day_realized_pnl_usd <= -limits.max_daily_loss_usd:
            account.kill_switch = True
            return RiskDecision(False, "daily loss kill switch")
        if account.cooldown_until is not None and req.now < account.cooldown_until:
            return RiskDecision(False, "cooldown after consecutive losses")
        for pos in account.open_positions:
            if pos.pool_address == req.pool_address or pos.token_address == req.token_address:
                return RiskDecision(False, "already open: averaging down is not allowed")
        if len(account.open_positions) >= limits.max_concurrent_positions:
            return RiskDecision(False, "max concurrent positions")
        size = self.order_size()
        exposure = sum((p.cost_basis_usd for p in account.open_positions), Decimal(0))
        if exposure + size > limits.max_total_exposure_usd:
            return RiskDecision(False, "max total exposure")
        if req.mid_price <= 0 or req.liquidity_usd <= 0:
            return RiskDecision(False, "price or liquidity missing")
        try:
            entry_gas = gas_usd(req.chain)
            impact = price_impact(size, req.liquidity_usd)
        except (CostModelError, KeyError) as exc:
            return RiskDecision(False, f"cost model rejected the entry: {exc}")
        if impact * Decimal(10_000) > limits.max_slippage_bps:
            return RiskDecision(False, "estimated slippage above the cap")
        try:
            trip = round_trip_cost(notional=size, mid=req.mid_price, liquidity=req.liquidity_usd,
                                   fee_rate=req.fee_rate, gas_per_tx=entry_gas)
        except CostModelError as exc:
            return RiskDecision(False, f"cost model rejected the entry: {exc}")
        if account.cash_usd - limits.gas_reserve_usd < size + entry_gas:
            return RiskDecision(False, "cash would breach the gas reserve")
        stop_room = size * limits.stop_loss_pct
        if trip >= stop_room:
            return RiskDecision(False, "round trip costs exceed the stop distance")
        planned = stop_room + trip
        return RiskDecision(
            True, "approved", size_usd=size, stop_loss_pct=limits.stop_loss_pct,
            take_profit_pct=limits.take_profit_pct,
            max_hold_until=req.now + timedelta(minutes=limits.max_hold_minutes),
            planned_max_loss_usd=planned,
        )

    def register_close(self, pnl: Decimal, now: datetime, account: AccountSnapshot) -> None:
        """Update the loss streak, the cooldown and the daily kill switch. Does not size up."""
        account.day_realized_pnl_usd += pnl
        if pnl < 0:
            account.consecutive_losses += 1
            if account.consecutive_losses >= self.limits.cooldown_losses:
                until = now + timedelta(seconds=self.limits.cooldown_seconds)
                if account.cooldown_until is None or until > account.cooldown_until:
                    account.cooldown_until = until
        elif pnl > 0:
            account.consecutive_losses = 0
        if account.day_realized_pnl_usd <= -self.limits.max_daily_loss_usd:
            account.kill_switch = True


def stops_from_exec(exec_price: Decimal, limits: RiskLimits) -> tuple[Decimal, Decimal]:
    """Stop and target off the price actually paid, not the pre-trade mid."""
    return exec_price * (Decimal(1) - limits.stop_loss_pct), exec_price * (Decimal(1) + limits.take_profit_pct)
