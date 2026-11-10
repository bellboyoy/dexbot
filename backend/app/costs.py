"""Swap fee, gas and constant-product slippage.

The pool is assumed to be a balanced two-asset AMM: each reserve is worth half of
`liquidity_usd`. Impact on a quote-sized trade of `q` is `q / (reserve + q)`.
That is the whole size dependence. There is no extra fixed floor, and there is no
historical liquidity series in the free APIs, so callers pass the liquidity they
actually have (for a backtest, that is today's snapshot applied to every bar).

Gas figures are conservative per-transaction estimates in USD, not live quotes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.domain.chains import Chain
from app.domain.market import PoolSnapshot

DEFAULT_SWAP_FEE = Decimal("0.003")  # 30 bps per side when the pool does not say
GAS_USD_PER_TX: dict[Chain, Decimal] = {
    Chain.SOLANA: Decimal("0.02"),
    Chain.BASE: Decimal("0.05"),
    Chain.BSC: Decimal("0.10"),
    Chain.ARBITRUM: Decimal("0.04"),
}
_FEE_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class CostModelError(ValueError):
    pass


def gas_usd(chain: Chain) -> Decimal:
    try:
        return GAS_USD_PER_TX[chain]
    except KeyError as exc:
        raise CostModelError(f"no gas estimate for {chain}") from exc


def fee_rate_for_snapshot(snap: PoolSnapshot, default: Decimal = DEFAULT_SWAP_FEE) -> Decimal:
    """Pool fee as a fraction of the input (0.003 = 0.30%).

    GeckoTerminal's `pool_fee_percentage` is a percent (verified null on live pools
    whose name carries the fee, e.g. "KNTQ / USDC 0.042% fee"). When the field is
    missing, a single percentage in the pool name is used. Otherwise `default`.
    """
    if snap.pool_fee_pct is not None and snap.pool_fee_pct >= 0:
        rate = snap.pool_fee_pct / Decimal(100)
        if Decimal(0) <= rate < 1:
            return rate
    name = ""
    raw = snap.raw or {}
    attrs = raw.get("attributes") if isinstance(raw, dict) else None
    if isinstance(attrs, dict) and attrs.get("name"):
        name = str(attrs["name"])
    found = _FEE_IN_NAME.findall(name)
    if found:
        pct = Decimal(found[-1])
        if Decimal(0) <= pct <= 10:
            return pct / Decimal(100)
    return default


def price_impact(amount_usd: Decimal, liquidity_usd: Decimal) -> Decimal:
    """Fraction of `amount_usd` lost to impact when it is added to one reserve."""
    if amount_usd <= 0 or liquidity_usd <= 0:
        raise CostModelError("amount and liquidity must be positive")
    reserve = liquidity_usd / Decimal(2)
    return amount_usd / (reserve + amount_usd)


@dataclass(frozen=True, slots=True)
class Fill:
    side: str                      # "buy" or "sell"
    mid: Decimal
    exec_price: Decimal
    qty: Decimal
    fee_usd: Decimal
    gas_usd: Decimal
    slippage_usd: Decimal
    impact: Decimal
    quote_spent: Decimal | None    # buy: cash into the swap, fee included
    quote_received: Decimal | None # sell: cash out of the swap, after fee, before gas

    def impact_bps(self) -> int:
        return int((self.impact * Decimal(10_000)).to_integral_value())


def buy_fill(*, mid: Decimal, notional: Decimal, liquidity: Decimal, fee_rate: Decimal, gas_usd: Decimal) -> Fill:
    """Spend `notional` quote USD. The fee is taken before the tokens hit the pool."""
    _check(mid, liquidity, fee_rate)
    if notional <= 0:
        raise CostModelError("notional must be positive")
    q = notional * (Decimal(1) - fee_rate)
    reserve = liquidity / Decimal(2)
    impact = q / (reserve + q)
    value_at_mid = q * reserve / (reserve + q)
    qty = value_at_mid / mid
    if qty <= 0:
        raise CostModelError("buy produced no tokens")
    return Fill(
        side="buy", mid=mid, exec_price=notional / qty, qty=qty,
        fee_usd=notional * fee_rate, gas_usd=gas_usd, slippage_usd=q - value_at_mid,
        impact=impact, quote_spent=notional, quote_received=None,
    )


def sell_fill(*, qty: Decimal, mid: Decimal, liquidity: Decimal, fee_rate: Decimal, gas_usd: Decimal) -> Fill:
    """Sell `qty` tokens. The fee is taken from the quote that leaves the pool."""
    _check(mid, liquidity, fee_rate)
    if qty <= 0:
        raise CostModelError("qty must be positive")
    gross = qty * mid
    reserve = liquidity / Decimal(2)
    impact = gross / (reserve + gross)
    after_impact = gross * reserve / (reserve + gross)
    fee = after_impact * fee_rate
    proceeds = after_impact - fee
    return Fill(
        side="sell", mid=mid, exec_price=proceeds / qty, qty=qty,
        fee_usd=fee, gas_usd=gas_usd, slippage_usd=gross - after_impact,
        impact=impact, quote_spent=None, quote_received=proceeds,
    )


def realized_pnl(cost_basis: Decimal, sell: Fill) -> Decimal:
    """Cash from the sell, after its gas, minus what the position cost to open.

    `cost_basis` is quote spent on the buy plus the entry gas. The sell's fee is
    already inside `quote_received`.
    """
    if sell.quote_received is None:
        raise CostModelError("realized P&L needs a sell fill")
    return sell.quote_received - sell.gas_usd - cost_basis


def unrealized_pnl(*, cost_basis: Decimal, qty: Decimal, mid: Decimal, liquidity: Decimal,
                   fee_rate: Decimal, exit_gas: Decimal) -> Decimal:
    """Mark to an estimated exit. Negative just after entry: the exit is not free."""
    return realized_pnl(cost_basis, sell_fill(qty=qty, mid=mid, liquidity=liquidity,
                                              fee_rate=fee_rate, gas_usd=exit_gas))


def round_trip_cost(*, notional: Decimal, mid: Decimal, liquidity: Decimal, fee_rate: Decimal,
                    gas_per_tx: Decimal) -> Decimal:
    """What a round trip loses if the mid does not move. Always >= both gas legs."""
    buy = buy_fill(mid=mid, notional=notional, liquidity=liquidity, fee_rate=fee_rate, gas_usd=gas_per_tx)
    sell = sell_fill(qty=buy.qty, mid=mid, liquidity=liquidity, fee_rate=fee_rate, gas_usd=gas_per_tx)
    return (notional + gas_per_tx) - (sell.quote_received - gas_per_tx)  # type: ignore[operator]


def _check(mid: Decimal, liquidity: Decimal, fee_rate: Decimal) -> None:
    if mid <= 0 or liquidity <= 0:
        raise CostModelError("mid and liquidity must be positive")
    if fee_rate < 0 or fee_rate >= 1:
        raise CostModelError("fee rate must be in [0, 1)")
