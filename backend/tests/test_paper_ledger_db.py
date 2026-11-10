"""Paper rows in Postgres match the in-memory P&L identity."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.costs import buy_fill, realized_pnl, sell_fill
from app.db.models import Base
from app.db.session import create_schema, make_engine, make_sessionmaker
from app.domain.chains import Chain
from app.domain.market import DataSource, PoolSnapshot, TokenRef
from app.paper.repository import apply_buy, apply_sell, build_summary, ensure_pool, ensure_token, get_or_create_account

URL = os.environ.get("DEXBOT_TEST_DATABASE_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not URL, reason="DEXBOT_TEST_DATABASE_URL not set")]
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
async def session():
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    async with make_sessionmaker(engine)() as made:
        yield made
    await engine.dispose()


async def test_persisted_round_trip_matches_the_ledger_math(session):
    account = await get_or_create_account(session, Decimal("1000"), now=NOW)
    base = await ensure_token(session, Chain.BASE, "0x" + "11" * 20, "TOK", "Tok")
    quote = await ensure_token(session, Chain.BASE, "0x" + "22" * 20, "USDC", "USDC")
    snap = PoolSnapshot(
        source=DataSource.DEXSCREENER, chain=Chain.BASE, pool_address="0x" + "33" * 20, dex_id="uniswap",
        base_token=TokenRef(Chain.BASE, base.address, "TOK"), quote_token=TokenRef(Chain.BASE, quote.address, "USDC"),
        observed_at=NOW, price_usd=Decimal("2"), price_native=None, liquidity_usd=Decimal("100000"),
        fdv_usd=None, market_cap_usd=None, pool_created_at=NOW - timedelta(days=3),
    )
    pool = await ensure_pool(session, snap, base, quote)
    buy = buy_fill(mid=Decimal("2"), notional=Decimal("50"), liquidity=Decimal("100000"),
                   fee_rate=Decimal("0.003"), gas_usd=Decimal("0.05"))
    position = await apply_buy(
        session, account, chain=Chain.BASE, token=base, pool=pool, strategy="momentum", fill=buy,
        stop=buy.exec_price * Decimal("0.92"), take_profit=buy.exec_price * Decimal("1.15"),
        max_hold_until=NOW + timedelta(hours=4), planned_max_loss=Decimal("8"), liquidity=Decimal("100000"),
        fee_rate=Decimal("0.003"), now=NOW,
    )
    await session.commit()
    summary = await build_summary(session, gas_reserve=Decimal("5"))
    assert Decimal(summary["balance_usd"]) == Decimal("1000") + Decimal(summary["net_pnl_usd"])
    assert Decimal(summary["unrealized_pnl_usd"]) < 0
    assert summary["realized_pnl_usd"] == "0"
    assert summary["mode"] == "paper"

    sell = sell_fill(qty=position.token_qty, mid=Decimal("2.4"), liquidity=Decimal("100000"),
                     fee_rate=Decimal("0.003"), gas_usd=Decimal("0.05"))
    pnl = await apply_sell(session, account, position, sell, reason="take_profit", now=NOW)
    await session.commit()
    assert pnl == realized_pnl(buy.quote_spent + buy.gas_usd, sell)
    closed = await build_summary(session, gas_reserve=Decimal("5"))
    assert closed["trade_count"] == 1
    assert Decimal(closed["cash_usd"]) == Decimal("1000") + Decimal(closed["realized_pnl_usd"])
    assert Decimal(closed["balance_usd"]) == Decimal(closed["cash_usd"])
    assert closed["open_positions"] == []
    assert closed["wins"] == (1 if pnl > 0 else 0)
