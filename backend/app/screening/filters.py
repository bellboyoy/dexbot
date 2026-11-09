"""Market filters that do not need a security vendor: liquidity, volume, age, exitability."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.config import Settings
from app.costs import CostModelError, price_impact
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.screening.models import Check, CheckStatus

# Quote assets we will accept. Native gas token on EVM is the zero address
# (GeckoTerminal uses it for ETH/BNB-quoted pools). Verified against live ids.
_QUOTES: dict[Chain, frozenset[str]] = {
    Chain.SOLANA: frozenset({
        "So11111111111111111111111111111111111111112",  # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }),
    Chain.BASE: frozenset({
        "0x0000000000000000000000000000000000000000",  # native ETH
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    }),
    Chain.BSC: frozenset({
        "0x0000000000000000000000000000000000000000",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH
    }),
    Chain.ARBITRUM: frozenset({
        "0x0000000000000000000000000000000000000000",
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
    }),
}
_STABLES: dict[Chain, frozenset[str]] = {
    Chain.SOLANA: frozenset({
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    }),
    Chain.BASE: frozenset({
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    }),
    Chain.BSC: frozenset({
        "0x55d398326f99059ff775485246999027b3197955",
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    }),
    Chain.ARBITRUM: frozenset({
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
    }),
}
_GAS: dict[Chain, frozenset[str]] = {
    Chain.SOLANA: frozenset({"So11111111111111111111111111111111111111112"}),
    Chain.BASE: frozenset({
        "0x0000000000000000000000000000000000000000",
        "0x4200000000000000000000000000000000000006",
    }),
    Chain.BSC: frozenset({
        "0x0000000000000000000000000000000000000000",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    }),
    Chain.ARBITRUM: frozenset({
        "0x0000000000000000000000000000000000000000",
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    }),
}


def _canon(chain: Chain, address: str) -> str:
    return address if chain is Chain.SOLANA else address.lower()


def quote_allowed(chain: Chain, address: str) -> bool:
    return _canon(chain, address) in _QUOTES.get(chain, frozenset())


def is_stable(chain: Chain, address: str) -> bool:
    return _canon(chain, address) in _STABLES.get(chain, frozenset())


def is_gas_token(chain: Chain, address: str) -> bool:
    return _canon(chain, address) in _GAS.get(chain, frozenset())


@dataclass(frozen=True, slots=True)
class ScreenThresholds:
    min_liquidity_usd: Decimal = Decimal("20000")
    min_volume_h1_usd: Decimal = Decimal("5000")
    min_age_minutes: int = 30
    max_tax: Decimal = Decimal("0.10")
    lp_lock_young_hours: int = 72
    min_lp_locked_pct: Decimal = Decimal("80")
    max_slippage_bps: int = 200
    position_usd: Decimal = Decimal("50")


def thresholds_from_settings(settings: Settings) -> ScreenThresholds:
    return ScreenThresholds(
        min_liquidity_usd=settings.screen_min_liquidity_usd,
        min_volume_h1_usd=settings.screen_min_volume_h1_usd,
        min_age_minutes=settings.screen_min_age_minutes,
        max_tax=settings.screen_max_tax,
        lp_lock_young_hours=settings.screen_lp_lock_young_hours,
        min_lp_locked_pct=settings.screen_min_lp_locked_pct,
        max_slippage_bps=settings.risk_max_slippage_bps,
        position_usd=settings.risk_max_position_usd,
    )


def _check(name: str, status: CheckStatus, critical: bool, detail: str) -> Check:
    return Check(name, status, critical, detail)


def market_checks(snap: PoolSnapshot, thresholds: ScreenThresholds, *, now: datetime) -> list[Check]:
    out: list[Check] = []
    price = snap.price_usd
    if price is None or price <= 0:
        out.append(_check("price", CheckStatus.UNKNOWN if price is None else CheckStatus.FAIL, True,
                          "price missing" if price is None else "price is not positive"))
    else:
        out.append(_check("price", CheckStatus.PASS, True, str(price)))

    quote = snap.quote_token.address if snap.quote_token else None
    if quote is None:
        out.append(_check("quote_asset", CheckStatus.FAIL, True, "pool has no quote token"))
    elif not quote_allowed(snap.chain, quote):
        out.append(_check("quote_asset", CheckStatus.FAIL, True, "quote is not a liquid major"))
    else:
        out.append(_check("quote_asset", CheckStatus.PASS, True, quote))
    if is_stable(snap.chain, snap.base_token.address):
        out.append(_check("base_asset", CheckStatus.FAIL, True, "base is a stablecoin"))
    else:
        out.append(_check("base_asset", CheckStatus.PASS, True, snap.base_token.symbol or snap.base_token.address))

    liq = snap.liquidity_usd
    if liq is None:
        out.append(_check("min_liquidity", CheckStatus.UNKNOWN, True, "liquidity missing"))
    elif liq < thresholds.min_liquidity_usd:
        out.append(_check("min_liquidity", CheckStatus.FAIL, True, f"{liq} < {thresholds.min_liquidity_usd}"))
    else:
        out.append(_check("min_liquidity", CheckStatus.PASS, True, str(liq)))

    h1 = snap.window("h1").volume_usd
    h24 = snap.window("h24").volume_usd
    vol = h1 if h1 is not None else (h24 / Decimal(24) if h24 is not None else None)
    if vol is None:
        out.append(_check("min_volume", CheckStatus.UNKNOWN, True, "h1 and h24 volume missing"))
    elif vol < thresholds.min_volume_h1_usd:
        out.append(_check("min_volume", CheckStatus.FAIL, True, f"{vol} < {thresholds.min_volume_h1_usd}"))
    else:
        out.append(_check("min_volume", CheckStatus.PASS, True, str(vol)))

    age = snap.pool_age(now)
    if age is None:
        out.append(_check("min_age", CheckStatus.UNKNOWN, True, "pool creation time missing"))
    elif age.total_seconds() < 0:
        out.append(_check("min_age", CheckStatus.FAIL, True, "pool timestamp is in the future"))
    elif age.total_seconds() < thresholds.min_age_minutes * 60:
        out.append(_check("min_age", CheckStatus.FAIL, True, f"age {age} < {thresholds.min_age_minutes}m"))
    else:
        out.append(_check("min_age", CheckStatus.PASS, True, str(age)))

    window = snap.window("h1")
    if window.buys is None and window.sells is None:
        window = snap.window("m5")
    if window.sells is None and window.buys is None:
        out.append(_check("recent_sells", CheckStatus.UNKNOWN, True, "no buy/sell counts"))
    elif window.sells is not None and window.sells > 0:
        out.append(_check("recent_sells", CheckStatus.PASS, True, f"sells={window.sells}"))
    elif (window.buys or 0) > 0:
        out.append(_check("recent_sells", CheckStatus.FAIL, True, "buys printing with zero sells"))
    else:
        out.append(_check("recent_sells", CheckStatus.UNKNOWN, True, "no trades in the window"))

    if liq is not None and liq > 0:
        try:
            impact = price_impact(thresholds.position_usd, liq)
        except CostModelError:
            out.append(_check("exit_slippage", CheckStatus.UNKNOWN, True, "could not estimate impact"))
        else:
            bps = impact * Decimal(10_000)
            if bps > thresholds.max_slippage_bps:
                out.append(_check("exit_slippage", CheckStatus.FAIL, True,
                                  f"impact {bps:.0f} bps > {thresholds.max_slippage_bps}"))
            else:
                out.append(_check("exit_slippage", CheckStatus.PASS, True, f"{bps:.0f} bps"))
    else:
        out.append(_check("exit_slippage", CheckStatus.UNKNOWN, True, "liquidity missing"))
    return out


def pool_is_young(snap: PoolSnapshot, thresholds: ScreenThresholds, now: datetime) -> bool:
    age = snap.pool_age(now)
    if age is None:
        return True
    return age.total_seconds() < thresholds.lp_lock_young_hours * 3600
