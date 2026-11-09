"""Persist the paper ledger. Raw on-chain amounts are 0: this process never builds a transaction."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.costs import Fill, realized_pnl, unrealized_pnl
from app.db.models import PaperAccount, Pool, Position, RiskEvent, SecurityCheck, Signal, Token, Trade, Transaction
from app.db.session import make_engine, make_sessionmaker
from app.domain.chains import Chain
from app.domain.market import PoolSnapshot
from app.screening.models import ScreenDecision

_engines: dict[str, tuple[AsyncEngine, async_sessionmaker]] = {}


def _factory(url: str) -> tuple[AsyncEngine, async_sessionmaker]:
    pair = _engines.get(url)
    if pair is None:
        engine = make_engine(url)
        pair = (engine, make_sessionmaker(engine))
        _engines[url] = pair
    return pair


@asynccontextmanager
async def session_scope(url: str):
    _engine, maker = _factory(url)
    async with maker() as session:
        yield session


def dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


async def get_or_create_account(session: AsyncSession, starting: Decimal, *, now: datetime | None = None) -> PaperAccount:
    account = await session.scalar(select(PaperAccount).where(PaperAccount.name == "default"))
    if account is not None:
        if account.mode != "paper":
            raise RuntimeError("refusing to touch an account whose mode is not paper")
        return account
    now = now or datetime.now(timezone.utc)
    account = PaperAccount(
        name="default", mode="paper", starting_balance_usd=starting, cash_usd=starting,
        realized_pnl_usd=Decimal(0), fees_usd=Decimal(0), gas_usd=Decimal(0), slippage_usd=Decimal(0),
        day_utc=now.date(), day_realized_pnl_usd=Decimal(0), consecutive_losses=0, cooldown_until=None,
        kill_switch=False, kill_reason=None, scan_count=0,
    )
    session.add(account)
    await session.flush()
    return account


def roll_day(account: PaperAccount, today) -> None:
    if account.day_utc == today:
        return
    account.day_utc = today
    account.day_realized_pnl_usd = Decimal(0)
    if account.kill_reason == "daily_loss":
        account.kill_switch = False
        account.kill_reason = None


async def ensure_token(session: AsyncSession, chain: Chain, address: str, symbol: str | None, name: str | None) -> Token:
    row = await session.scalar(select(Token).where(Token.chain == chain, Token.address == address))
    if row is None:
        row = Token(chain=chain, address=address, symbol=symbol, name=name, is_validated=False)
        session.add(row)
        await session.flush()
    elif symbol and not row.symbol:
        row.symbol = symbol
    return row


async def ensure_pool(session: AsyncSession, snap: PoolSnapshot, base: Token, quote: Token | None) -> Pool:
    row = await session.scalar(select(Pool).where(Pool.chain == snap.chain, Pool.address == snap.pool_address))
    if row is None:
        row = Pool(
            chain=snap.chain, address=snap.pool_address, dex_id=snap.dex_id, base_token_id=base.id,
            quote_token_id=quote.id if quote else None, pool_created_at=snap.pool_created_at,
        )
        session.add(row)
        await session.flush()
    return row


async def record_screen(session: AsyncSession, snap: PoolSnapshot, decision: ScreenDecision, *,
                        strategy: str, now: datetime) -> Signal:
    base = await ensure_token(session, snap.chain, snap.base_token.address, snap.base_token.symbol, snap.base_token.name)
    quote = None
    if snap.quote_token is not None:
        quote = await ensure_token(session, snap.chain, snap.quote_token.address, snap.quote_token.symbol, snap.quote_token.name)
    pool = await ensure_pool(session, snap, base, quote)
    for check in decision.checks:
        if check.name in {"min_liquidity", "min_volume", "min_age", "price", "quote_asset", "base_asset", "recent_sells", "exit_slippage"}:
            continue
        session.add(SecurityCheck(
            token_id=base.id, chain=snap.chain, provider=check.name.split("_", 1)[0], check_name=check.name[:64],
            status=check.status.value, critical=check.critical, details={"detail": check.detail[:500]}, checked_at=now,
        ))
    signal = Signal(
        chain=snap.chain, pool_id=pool.id, strategy=strategy[:64], strategy_version="screen",
        decision=("accepted" if decision.allowed else "rejected_screen"),
        decision_reason=decision.reason[:1000], features={"symbol": snap.base_token.symbol}, generated_at=now,
    )
    session.add(signal)
    await session.flush()
    return signal


async def load_open(session: AsyncSession) -> list[tuple[Position, Token, Pool]]:
    rows = (await session.execute(
        select(Position, Token, Pool)
        .join(Token, Position.token_id == Token.id)
        .join(Pool, Position.pool_id == Pool.id)
        .where(Position.mode == "paper", Position.status == "open")
    )).all()
    return [(pos, token, pool) for pos, token, pool in rows]


def _trade(position: Position, fill: Fill, *, when: datetime) -> Trade:
    return Trade(
        position_id=position.id, chain=position.chain, mode="paper", side=fill.side,
        amount_in_raw=Decimal(0), amount_out_raw=Decimal(0), price_usd=fill.exec_price,
        fees_usd=fill.fee_usd, gas_usd=fill.gas_usd, slippage_bps=fill.impact_bps(),
        price_impact_bps=fill.impact_bps(), slippage_usd=fill.slippage_usd, executed_at=when,
    )


async def apply_buy(session: AsyncSession, account: PaperAccount, *, chain: Chain, token: Token, pool: Pool,
                    strategy: str, fill: Fill, stop: Decimal, take_profit: Decimal, max_hold_until: datetime,
                    planned_max_loss: Decimal, liquidity: Decimal, fee_rate: Decimal, now: datetime,
                    signal_id: int | None = None) -> Position:
    if fill.quote_spent is None:
        raise ValueError("buy fill is missing quote spent")
    cost_basis = fill.quote_spent + fill.gas_usd
    unreal = unrealized_pnl(cost_basis=cost_basis, qty=fill.qty, mid=fill.mid, liquidity=liquidity,
                            fee_rate=fee_rate, exit_gas=fill.gas_usd)
    account.cash_usd -= cost_basis
    account.fees_usd += fill.fee_usd
    account.gas_usd += fill.gas_usd
    account.slippage_usd += fill.slippage_usd
    position = Position(
        mode="paper", chain=chain, token_id=token.id, pool_id=pool.id, signal_id=signal_id, strategy=strategy,
        status="open", size_usd=fill.quote_spent, token_qty=fill.qty, entry_price_usd=fill.exec_price,
        planned_max_loss_usd=planned_max_loss, stop_loss_price_usd=stop, take_profit_price_usd=take_profit,
        max_hold_until=max_hold_until, opened_at=now, fees_usd=fill.fee_usd, gas_usd=fill.gas_usd,
        mark_price_usd=fill.mid, unrealized_pnl_usd=unreal, entry_liquidity_usd=liquidity, fee_rate=fee_rate,
    )
    session.add(position)
    await session.flush()
    session.add(_trade(position, fill, when=now))
    session.add(Transaction(
        chain=chain, mode="paper", position_id=position.id, idempotency_key=f"paper:{position.id}:buy",
        status="simulated", details={"note": "paper fill, no chain transaction"},
    ))
    return position


async def apply_sell(session: AsyncSession, account: PaperAccount, position: Position, fill: Fill, *,
                     reason: str, now: datetime) -> Decimal:
    if fill.quote_received is None or position.token_qty is None:
        raise ValueError("cannot close a position without a quantity and a sell fill")
    cost_basis = position.size_usd + position.gas_usd
    pnl = realized_pnl(cost_basis, fill)
    account.cash_usd += fill.quote_received - fill.gas_usd
    account.realized_pnl_usd += pnl
    account.fees_usd += fill.fee_usd
    account.gas_usd += fill.gas_usd
    account.slippage_usd += fill.slippage_usd
    position.fees_usd += fill.fee_usd
    position.gas_usd += fill.gas_usd
    position.exit_price_usd = fill.exec_price
    position.closed_at = now
    position.exit_reason = reason
    position.net_pnl_usd = pnl
    position.unrealized_pnl_usd = None
    position.mark_price_usd = fill.mid
    position.status = "closed"
    session.add(_trade(position, fill, when=now))
    session.add(Transaction(
        chain=position.chain, mode="paper", position_id=position.id, idempotency_key=f"paper:{position.id}:sell",
        status="simulated", details={"reason": reason},
    ))
    return pnl


def mark_position(position: Position, *, mid: Decimal, liquidity: Decimal) -> None:
    if position.token_qty is None or position.fee_rate is None:
        return
    exit_gas = position.gas_usd  # entry gas was stored; the exit estimate uses the same per-tx figure
    # gas_usd on an open position is the entry gas, which equals one transaction.
    position.mark_price_usd = mid
    position.unrealized_pnl_usd = unrealized_pnl(
        cost_basis=position.size_usd + position.gas_usd, qty=position.token_qty, mid=mid,
        liquidity=liquidity, fee_rate=position.fee_rate, exit_gas=exit_gas,
    )


async def build_summary(session: AsyncSession, *, gas_reserve: Decimal, known_strategies: list[str] | None = None) -> dict:
    account = await session.scalar(select(PaperAccount).where(PaperAccount.name == "default"))
    positions = (await session.scalars(select(Position).where(Position.mode == "paper"))).all()
    tokens = {row.id: row for row in (await session.scalars(select(Token))).all()}
    pools = {row.id: row for row in (await session.scalars(select(Pool))).all()}
    if account is None:
        return _empty_summary(gas_reserve)
    closed = [p for p in positions if p.status == "closed" and p.net_pnl_usd is not None]
    opens = [p for p in positions if p.status == "open"]
    wins = [p.net_pnl_usd for p in closed if p.net_pnl_usd > 0]
    losses = [p.net_pnl_usd for p in closed if p.net_pnl_usd < 0]
    unrealized_known = all(p.unrealized_pnl_usd is not None for p in opens)
    unrealized = sum((p.unrealized_pnl_usd for p in opens), Decimal(0)) if unrealized_known else None
    deployed = sum((p.size_usd + p.gas_usd for p in opens), Decimal(0))
    net = (account.realized_pnl_usd + unrealized) if unrealized is not None else None
    equity = (account.cash_usd + deployed + unrealized) if unrealized is not None else None
    available = account.cash_usd - gas_reserve
    if available < 0:
        available = Decimal(0)

    def stats(rows: list[Position]) -> dict:
        w = [p.net_pnl_usd for p in rows if p.net_pnl_usd is not None and p.net_pnl_usd > 0]
        l = [p.net_pnl_usd for p in rows if p.net_pnl_usd is not None and p.net_pnl_usd < 0]
        nets = [p.net_pnl_usd for p in rows if p.net_pnl_usd is not None]
        return {
            "trades": len(nets),
            "wins": len(w),
            "losses": len(l),
            "win_rate": dec_str(Decimal(len(w)) / Decimal(len(nets))) if nets else None,
            "average_win_usd": dec_str(sum(w, Decimal(0)) / Decimal(len(w))) if w else None,
            "average_loss_usd": dec_str(sum(l, Decimal(0)) / Decimal(len(l))) if l else None,
            "net_pnl_usd": dec_str(sum(nets, Decimal(0))) if nets else "0",
            "fees_usd": dec_str(sum((p.fees_usd for p in rows), Decimal(0))),
            "gas_usd": dec_str(sum((p.gas_usd for p in rows), Decimal(0))),
            "open_positions": sum(1 for p in rows if p.status == "open"),
        }

    by_name: dict[str, list[Position]] = {}
    for pos in positions:
        by_name.setdefault(pos.strategy, []).append(pos)
    for name in known_strategies or []:
        by_name.setdefault(name, [])
    open_rows = []
    for pos in opens:
        token = tokens.get(pos.token_id)
        pool = pools.get(pos.pool_id)
        open_rows.append({
            "id": pos.id,
            "strategy": pos.strategy,
            "chain": pos.chain.value,
            "pool": pool.address if pool else None,
            "symbol": token.symbol if token else None,
            "size_usd": dec_str(pos.size_usd),
            "entry_price_usd": dec_str(pos.entry_price_usd),
            "mark_price_usd": dec_str(pos.mark_price_usd),
            "stop_loss_price_usd": dec_str(pos.stop_loss_price_usd),
            "take_profit_price_usd": dec_str(pos.take_profit_price_usd),
            "unrealized_pnl_usd": dec_str(pos.unrealized_pnl_usd),
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            "max_hold_until": pos.max_hold_until.isoformat() if pos.max_hold_until else None,
        })
    return {
        "mode": "paper",
        "label": "PAPER",
        "balance_usd": dec_str(equity),
        "cash_usd": dec_str(account.cash_usd),
        "available_capital_usd": dec_str(available),
        "capital_deployed_usd": dec_str(deployed),
        "realized_pnl_usd": dec_str(account.realized_pnl_usd),
        "unrealized_pnl_usd": dec_str(unrealized),
        "net_pnl_usd": dec_str(net),
        "starting_balance_usd": dec_str(account.starting_balance_usd),
        "trade_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": dec_str(Decimal(len(wins)) / Decimal(len(closed))) if closed else None,
        "average_win_usd": dec_str(sum(wins, Decimal(0)) / Decimal(len(wins))) if wins else None,
        "average_loss_usd": dec_str(sum(losses, Decimal(0)) / Decimal(len(losses))) if losses else None,
        "fees_usd": dec_str(account.fees_usd),
        "gas_usd": dec_str(account.gas_usd),
        "slippage_usd": dec_str(account.slippage_usd),
        "open_positions": open_rows,
        "strategies": [{"strategy": name, **stats(rows)} for name, rows in sorted(by_name.items())],
        "system": {
            "trading_mode": "paper",
            "execution": "disabled",
            "kill_switch": account.kill_switch,
            "kill_reason": account.kill_reason,
            "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else None,
            "consecutive_losses": account.consecutive_losses,
            "day_realized_pnl_usd": dec_str(account.day_realized_pnl_usd),
            "gas_reserve_usd": dec_str(gas_reserve),
            "last_scan_at": account.last_scan_at.isoformat() if account.last_scan_at else None,
            "last_error": account.last_error,
            "scan_count": account.scan_count,
            "last_cycle": account.last_cycle,
        },
    }


def _empty_summary(gas_reserve: Decimal) -> dict:
    return {
        "mode": "paper", "label": "PAPER", "balance_usd": None, "cash_usd": None,
        "available_capital_usd": None, "capital_deployed_usd": "0", "realized_pnl_usd": "0",
        "unrealized_pnl_usd": "0", "net_pnl_usd": "0", "starting_balance_usd": None,
        "trade_count": 0, "wins": 0, "losses": 0, "win_rate": None, "average_win_usd": None,
        "average_loss_usd": None, "fees_usd": "0", "gas_usd": "0", "slippage_usd": "0",
        "open_positions": [], "strategies": [],
        "system": {"trading_mode": "paper", "execution": "disabled", "kill_switch": False,
                   "gas_reserve_usd": dec_str(gas_reserve), "message": "paper account not created yet"},
    }


async def add_risk_event(session: AsyncSession, event_type: str, severity: str, *, chain: Chain | None = None,
                         position_id: int | None = None, details: dict | None = None) -> None:
    session.add(RiskEvent(event_type=event_type, severity=severity, chain=chain, position_id=position_id, details=details))
