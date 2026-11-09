"""Screening profiles for the collection watchlist.

These decide which pools get their history recorded. They are deliberately looser than
the trade filters (Phase 3–4): to measure a strategy honestly we need data on tokens
that later fail, not only on winners. Thresholds are initial, uncalibrated values.

Unknown data never passes a check: a missing liquidity figure is a rejection reason,
not a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.domain.assets import QUOTE_ASSETS
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot


class Profile(StrEnum):
    NEW = "new"
    ESTABLISHED = "established"


@dataclass(frozen=True)
class NewTokenProfile:
    max_age: timedelta = timedelta(hours=72)
    min_age: timedelta = timedelta(minutes=5)          # skip the first launch minutes
    min_liquidity_usd: Decimal = Decimal(5_000)
    min_h1_txns: int = 20
    require_sells: bool = True                         # buys with zero sells = possible honeypot


@dataclass(frozen=True)
class EstablishedTokenProfile:
    min_liquidity_usd: Decimal = Decimal(100_000)
    min_h24_volume_usd: Decimal = Decimal(100_000)
    min_h24_txns: int = 300


@dataclass(frozen=True)
class ScreeningConfig:
    new: NewTokenProfile = field(default_factory=NewTokenProfile)
    established: EstablishedTokenProfile = field(default_factory=EstablishedTokenProfile)
    # Quote assets confirmed live by validate_quote_assets. None = use the static registry
    # (tests only; the collector always passes verified addresses).
    verified_quotes: dict[Chain, set[str]] | None = None

    def quote_allowed(self, chain: Chain, address: str | None) -> bool:
        if address is None:
            return False
        allowed = (self.verified_quotes.get(chain, set()) if self.verified_quotes is not None
                   else set(QUOTE_ASSETS.get(chain, {})))
        return address in allowed


@dataclass(frozen=True)
class ScreenResult:
    passed: bool
    profile: Profile | None
    rejections: list[str]
    metrics: dict[str, Any]


def _txns(snap: PoolSnapshot, window: str) -> int | None:
    w = snap.window(window)
    return None if w.buys is None or w.sells is None else w.buys + w.sells


def classify(snap: PoolSnapshot, now: datetime, cfg: ScreeningConfig) -> Profile | None:
    age = snap.pool_age(now)
    if age is None:
        return None
    return Profile.NEW if age <= cfg.new.max_age else Profile.ESTABLISHED


def screen(snap: PoolSnapshot, now: datetime, cfg: ScreeningConfig | None = None) -> ScreenResult:
    cfg = cfg or ScreeningConfig()
    rejections: list[str] = []
    age = snap.pool_age(now)
    metrics: dict[str, Any] = {
        "liquidity_usd": snap.liquidity_usd,
        "pool_age_min": None if age is None else round(age.total_seconds() / 60, 1),
        "h1_txns": _txns(snap, "h1"),
        "h24_txns": _txns(snap, "h24"),
        "h24_volume_usd": snap.window("h24").volume_usd,
        "quote": snap.quote_token.symbol if snap.quote_token else None,
        "source": snap.source.value,
    }

    if not cfg.quote_allowed(snap.chain, snap.quote_token.address if snap.quote_token else None):
        rejections.append("quote_not_exitable")

    profile = classify(snap, now, cfg)
    if profile is None:
        rejections.append("unknown_pool_age")
        return ScreenResult(False, None, rejections, metrics)

    liq = snap.liquidity_usd
    if profile is Profile.NEW:
        p = cfg.new
        if age < p.min_age:
            rejections.append("too_young")
        if liq is None:
            rejections.append("unknown_liquidity")
        elif liq < p.min_liquidity_usd:
            rejections.append("low_liquidity")
        h1 = _txns(snap, "h1")
        if h1 is None:
            rejections.append("unknown_activity")
        elif h1 < p.min_h1_txns:
            rejections.append("low_activity")
        if p.require_sells:
            w = snap.window("h1")
            if w.sells is None:
                rejections.append("unknown_sells")
            elif w.sells == 0 and (w.buys or 0) > 0:
                rejections.append("no_sells")
    else:
        p = cfg.established
        if liq is None:
            rejections.append("unknown_liquidity")
        elif liq < p.min_liquidity_usd:
            rejections.append("low_liquidity")
        vol = snap.window("h24").volume_usd
        if vol is None:
            rejections.append("unknown_volume")
        elif vol < p.min_h24_volume_usd:
            rejections.append("low_volume")
        t = _txns(snap, "h24")
        if t is None:
            rejections.append("unknown_activity")
        elif t < p.min_h24_txns:
            rejections.append("low_activity")

    return ScreenResult(not rejections, profile, rejections, metrics)
