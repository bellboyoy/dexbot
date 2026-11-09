"""Scan, screen and simulate. No wallet, no signing, no submission.

Stops are evaluated on the scan cadence, not on every swap in the pool: a wick between
scans is invisible. Marks that the providers cannot price are left unchanged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import TradingMode, get_settings
from app.costs import buy_fill, fee_rate_for_snapshot, gas_usd, sell_fill
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot, Timeframe
from app.logging import get_logger
from app.marketdata.errors import ProviderError
from app.paper.repository import (
    add_risk_event, apply_buy, apply_sell, ensure_pool, ensure_token, get_or_create_account,
    load_open, mark_position, record_screen, roll_day, session_scope,
)
from app.risk.engine import AccountSnapshot, EntryRequest, OpenExposure, RiskEngine, stops_from_exec
from app.risk.limits import RiskLimits, limits_from_settings
from app.screening.engine import ScreeningEngine
from app.screening.filters import ScreenThresholds, is_gas_token, is_stable, thresholds_from_settings
from app.strategy.base import Action
from app.strategy.registry import build_strategy
from app.strategy.validation import ValidationFile

log = get_logger(__name__)
TIMEFRAME = Timeframe.M5


def choose_exit(mid: Decimal, stop: Decimal | None, take_profit: Decimal | None,
                now: datetime, max_hold_until: datetime | None) -> str | None:
    if stop is not None and mid <= stop:
        return "stop_loss"
    if take_profit is not None and mid >= take_profit:
        return "take_profit"
    if max_hold_until is not None and now >= max_hold_until:
        return "time_stop"
    return None


@dataclass(slots=True)
class _Open:
    id: int
    chain: Chain
    pool_address: str
    token_address: str
    strategy: str
    size_usd: Decimal
    gas_usd: Decimal
    stop: Decimal | None
    take_profit: Decimal | None
    max_hold_until: datetime
    entry_liquidity: Decimal | None
    fee_rate: Decimal | None
    token_qty: Decimal | None


@dataclass(slots=True)
class _Exit:
    position_id: int
    mid: Decimal
    liquidity: Decimal
    reason: str


@dataclass(slots=True)
class _Mark:
    position_id: int
    mid: Decimal
    liquidity: Decimal


@dataclass(slots=True)
class _Entry:
    snap: PoolSnapshot
    strategy: str
    params: dict


def _exposure(rows: list[_Open]) -> list[OpenExposure]:
    return [OpenExposure(row.chain, row.pool_address, row.token_address, row.size_usd + row.gas_usd) for row in rows]


async def _price(market, chain: Chain, pool: str) -> PoolSnapshot | None:
    try:
        return await market.get_pool(chain, pool)
    except ProviderError as exc:
        log.warning("mark_failed", chain=chain.value, pool=pool, error=str(exc))
        return None


async def plan_cycle(*, market, screening: ScreeningEngine, risk: RiskEngine, limits: RiskLimits,
                     thresholds: ScreenThresholds, validation: ValidationFile, chains: list[Chain],
                     opens: list[_Open], scan_count: int, now: datetime,
                     max_security: int, max_ohlcv: int) -> dict:
    exits: list[_Exit] = []
    marks: list[_Mark] = []
    errors: list[str] = []
    for row in opens:
        snap = await _price(market, row.chain, row.pool_address)
        if snap is None or snap.price_usd is None or snap.price_usd <= 0:
            errors.append(f"no price for {row.pool_address}")
            continue
        liquidity = snap.liquidity_usd or row.entry_liquidity
        if liquidity is None or liquidity <= 0:
            errors.append(f"no liquidity for {row.pool_address}")
            continue
        reason = choose_exit(snap.price_usd, row.stop, row.take_profit, now, row.max_hold_until)
        if reason:
            exits.append(_Exit(row.id, snap.price_usd, liquidity, reason))
        else:
            marks.append(_Mark(row.id, snap.price_usd, liquidity))

    rotation = [(chain, kind) for chain in chains for kind in ("new", "trending")]
    chain, kind = rotation[scan_count % len(rotation)] if rotation else (Chain.SOLANA, "new")
    try:
        snaps = await (market.get_new_pools(chain) if kind == "new" else market.get_trending_pools(chain))
    except ProviderError as exc:
        errors.append(str(exc))
        snaps = []
    screened = 0
    market_rejected = 0
    security_rejected = 0
    survivors: list[PoolSnapshot] = []
    market_notes: list[tuple[PoolSnapshot, object]] = []
    for snap in snaps:
        if is_stable(snap.chain, snap.base_token.address) or is_gas_token(snap.chain, snap.base_token.address):
            continue
        screened += 1
        try:
            decision = await screening.screen(snap, now=now, with_security=False)
        except Exception as exc:
            errors.append(f"screen {snap.pool_address}: {type(exc).__name__}")
            continue
        if not decision.allowed:
            market_rejected += 1
            market_notes.append((snap, decision))
            continue
        survivors.append(snap)
    approved: list[tuple[PoolSnapshot, object]] = []
    for snap in sorted(survivors, key=lambda s: s.liquidity_usd or Decimal(0), reverse=True)[:max_security]:
        try:
            decision = await screening.screen(snap, now=now, with_security=True)
        except Exception as exc:
            errors.append(f"security {snap.pool_address}: {type(exc).__name__}")
            continue
        market_notes.append((snap, decision))
        if decision.allowed:
            approved.append((snap, decision))
        else:
            security_rejected += 1
    entries: list[_Entry] = []
    if not validation.passed:
        note = "no strategy passed validation; entries are disabled"
    else:
        note = None
        scan_tf = Timeframe(validation.timeframe) if validation.timeframe else TIMEFRAME
        for snap, _decision in approved[:max_ohlcv]:
            try:
                candles = await market.get_ohlcv(snap.chain, snap.pool_address, scan_tf, 200)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            if len(candles) < 30:
                continue
            for name in validation.passed:
                params = (validation.configs or {}).get(name) or {}
                signal = build_strategy(name, params).on_bar(candles)
                if signal.action is Action.ENTER:
                    entries.append(_Entry(snap, name, params))
                    break
    return {
        "exits": exits, "marks": marks, "entries": entries, "screens": market_notes,
        "screened": screened, "market_rejected": market_rejected, "security_rejected": security_rejected,
        "errors": errors, "note": note, "source": f"{chain.value}:{kind}",
    }


async def apply_cycle(session, *, plan: dict, account_starting: Decimal, risk: RiskEngine, limits: RiskLimits,
                      now: datetime) -> None:
    account = await get_or_create_account(session, account_starting, now=now)
    roll_day(account, now.date())
    opens = await load_open(session)
    by_id = {pos.id: (pos, token, pool) for pos, token, pool in opens}
    views = [
        _Open(pos.id, pos.chain, pool.address, token.address, pos.strategy, pos.size_usd, pos.gas_usd,
              pos.stop_loss_price_usd, pos.take_profit_price_usd, pos.max_hold_until, pos.entry_liquidity_usd,
              pos.fee_rate, pos.token_qty)
        for pos, token, pool in opens
    ]
    exited: set[int] = set()
    for item in plan["exits"]:
        row = by_id.get(item.position_id)
        if row is None:
            continue
        pos, _token, _pool = row
        if pos.token_qty is None or pos.fee_rate is None:
            continue
        fill = sell_fill(qty=pos.token_qty, mid=item.mid, liquidity=item.liquidity,
                         fee_rate=pos.fee_rate, gas_usd=gas_usd(pos.chain))
        pnl = await apply_sell(session, account, pos, fill, reason=item.reason, now=now)
        exited.add(pos.id)
        snap = _account_snap(account, [v for v in views if v.id not in exited])
        risk.register_close(pnl, now, snap)
        _copy_risk(account, snap)
        if account.kill_switch and account.kill_reason is None:
            account.kill_reason = "daily_loss"
            await add_risk_event(session, "kill_switch", "critical", chain=pos.chain, position_id=pos.id,
                                 details={"reason": "daily_loss"})
    for item in plan["marks"]:
        if item.position_id in exited:
            continue
        row = by_id.get(item.position_id)
        if row is None:
            continue
        mark_position(row[0], mid=item.mid, liquidity=item.liquidity)
    entries = 0
    for screen_snap, decision in plan["screens"]:
        await record_screen(session, screen_snap, decision, strategy="screen", now=now)
    if plan.get("note"):
        pass
    else:
        fresh = await load_open(session)
        views = [
            _Open(pos.id, pos.chain, pool.address, token.address, pos.strategy, pos.size_usd, pos.gas_usd,
                  pos.stop_loss_price_usd, pos.take_profit_price_usd, pos.max_hold_until,
                  pos.entry_liquidity_usd, pos.fee_rate, pos.token_qty)
            for pos, token, pool in fresh
        ]
        for entry in plan["entries"]:
            snap = entry.snap
            if snap.price_usd is None or snap.liquidity_usd is None:
                continue
            account_snap = _account_snap(account, views)
            decision = risk.evaluate_entry(account_snap, EntryRequest(
                chain=snap.chain, pool_address=snap.pool_address, token_address=snap.base_token.address,
                strategy=entry.strategy, mid_price=snap.price_usd, liquidity_usd=snap.liquidity_usd,
                fee_rate=fee_rate_for_snapshot(snap), now=now,
            ))
            _copy_risk(account, account_snap)
            if not decision.approved or decision.size_usd is None:
                base = await ensure_token(session, snap.chain, snap.base_token.address, snap.base_token.symbol, snap.base_token.name)
                quote = None
                if snap.quote_token is not None:
                    quote = await ensure_token(session, snap.chain, snap.quote_token.address, snap.quote_token.symbol, snap.quote_token.name)
                pool = await ensure_pool(session, snap, base, quote)
                from app.db.models import Signal
                signal = Signal(
                    chain=snap.chain, pool_id=pool.id, strategy=entry.strategy, strategy_version="1.0.0",
                    decision="rejected_risk", decision_reason=decision.reason, generated_at=now,
                )
                session.add(signal)
                await add_risk_event(session, "entry_rejected", "warning", chain=snap.chain, details={"reason": decision.reason})
                continue
            fill = buy_fill(mid=snap.price_usd, notional=decision.size_usd, liquidity=snap.liquidity_usd,
                            fee_rate=fee_rate_for_snapshot(snap), gas_usd=gas_usd(snap.chain))
            stop, take = stops_from_exec(fill.exec_price, limits)
            hold = decision.max_hold_until
            strat = build_strategy(entry.strategy, entry.params)
            if strat.max_hold_bars:
                sooner = now + timedelta(minutes=TIMEFRAME.seconds // 60 * strat.max_hold_bars)
                if hold is None or sooner < hold:
                    hold = sooner
            base = await ensure_token(session, snap.chain, snap.base_token.address, snap.base_token.symbol, snap.base_token.name)
            quote = None
            if snap.quote_token is not None:
                quote = await ensure_token(session, snap.chain, snap.quote_token.address, snap.quote_token.symbol, snap.quote_token.name)
            pool = await ensure_pool(session, snap, base, quote)
            from app.db.models import Signal
            signal = Signal(
                chain=snap.chain, pool_id=pool.id, strategy=entry.strategy, strategy_version=strat.version,
                decision="accepted", decision_reason="paper entry", generated_at=now,
            )
            session.add(signal)
            await session.flush()
            position = await apply_buy(
                session, account, chain=snap.chain, token=base, pool=pool, strategy=entry.strategy, fill=fill,
                stop=stop, take_profit=take, max_hold_until=hold, planned_max_loss=decision.planned_max_loss_usd,
                liquidity=snap.liquidity_usd, fee_rate=fee_rate_for_snapshot(snap), now=now, signal_id=signal.id,
            )
            views.append(_Open(
                position.id, snap.chain, snap.pool_address, snap.base_token.address, entry.strategy,
                fill.quote_spent, fill.gas_usd, stop, take, hold, snap.liquidity_usd, fee_rate_for_snapshot(snap),
                fill.qty,
            ))
            entries += 1
    account.scan_count += 1
    account.last_scan_at = now
    account.last_error = "; ".join(plan["errors"])[:1000] if plan["errors"] else None
    account.last_cycle = {
        "source": plan.get("source"),
        "screened": plan["screened"],
        "market_rejected": plan["market_rejected"],
        "security_rejected": plan["security_rejected"],
        "entries": entries,
        "exits": len(plan["exits"]),
        "note": plan.get("note"),
    }


def _account_snap(account, views: list[_Open]) -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=account.cash_usd, day_realized_pnl_usd=account.day_realized_pnl_usd,
        consecutive_losses=account.consecutive_losses, cooldown_until=account.cooldown_until,
        kill_switch=account.kill_switch, open_positions=_exposure(views),
    )


def _copy_risk(account, snap: AccountSnapshot) -> None:
    account.day_realized_pnl_usd = snap.day_realized_pnl_usd
    account.consecutive_losses = snap.consecutive_losses
    account.cooldown_until = snap.cooldown_until
    if snap.kill_switch:
        account.kill_switch = True
        if not account.kill_reason:
            account.kill_reason = "daily_loss"


async def run_once(*, balance: Decimal | None = None) -> None:
    settings = get_settings()
    if settings.trading_mode is not TradingMode.PAPER:
        raise SystemExit("refusing to run: trading mode is not paper")
    from app.marketdata.aggregator import build_market_data_service
    from app.screening.providers import build_security_clients
    market = build_market_data_service(settings)
    clients = build_security_clients()
    screening = ScreeningEngine(clients, thresholds_from_settings(settings))
    limits = limits_from_settings(settings)
    risk = RiskEngine(limits)
    validation = _load()
    now = datetime.now(timezone.utc)
    starting = balance if balance is not None else settings.paper_starting_balance_usd
    try:
        async with session_scope(settings.database_url) as session:
            account = await get_or_create_account(session, starting, now=now)
            await session.commit()
            opens = await load_open(session)
            scan_count = account.scan_count
            views = [
                _Open(pos.id, pos.chain, pool.address, token.address, pos.strategy, pos.size_usd, pos.gas_usd,
                      pos.stop_loss_price_usd, pos.take_profit_price_usd, pos.max_hold_until,
                      pos.entry_liquidity_usd, pos.fee_rate, pos.token_qty)
                for pos, token, pool in opens
            ]
        plan = await plan_cycle(
            market=market, screening=screening, risk=risk, limits=limits,
            thresholds=thresholds_from_settings(settings), validation=validation,
            chains=list(settings.enabled_chains), opens=views, scan_count=scan_count, now=now,
            max_security=settings.paper_max_security_checks, max_ohlcv=settings.paper_max_ohlcv_per_cycle,
        )
        async with session_scope(settings.database_url) as session:
            async with session.begin():
                await apply_cycle(session, plan=plan, account_starting=starting, risk=risk, limits=limits, now=now)
        log.info("paper_cycle", source=plan.get("source"), entries=len(plan["entries"]), exits=len(plan["exits"]),
                 note=plan.get("note"))
    finally:
        await screening.aclose()
        await market.aclose()


def _load() -> ValidationFile:
    from app.strategy.validation import load_validation
    return load_validation()


async def run_loop(interval: float, balance: Decimal | None) -> None:
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await run_once(balance=balance)
        except Exception as exc:
            log.error("paper_cycle_failed", error=type(exc).__name__)
            await _record_error(str(exc)[:500])
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(1.0, interval - elapsed))


async def _record_error(message: str) -> None:
    settings = get_settings()
    try:
        async with session_scope(settings.database_url) as session:
            async with session.begin():
                account = await get_or_create_account(session, settings.paper_starting_balance_usd)
                account.last_error = message
                account.last_scan_at = datetime.now(timezone.utc)
    except Exception:
        log.error("paper_error_not_persisted")
