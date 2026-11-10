"""Fee, slippage, P&L identity and every risk limit."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import Settings
from app.costs import buy_fill, fee_rate_for_snapshot, price_impact, realized_pnl, round_trip_cost, sell_fill
from app.domain.chains import Chain
from app.domain.market import DataSource, PoolSnapshot, TokenRef
from app.paper.ledger import Ledger, OpenPos
from app.risk.engine import AccountSnapshot, EntryRequest, OpenExposure, RiskEngine, stops_from_exec
from app.risk.limits import RiskLimits, limits_from_settings

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
POOL = "0x" + "ab" * 20
TOKEN = "0x" + "cd" * 20


def test_buy_fill_matches_constant_product():
    fill = buy_fill(mid=Decimal("2"), notional=Decimal("50"), liquidity=Decimal("100000"),
                    fee_rate=Decimal("0.003"), gas_usd=Decimal("0.02"))
    q = Decimal("50") * Decimal("0.997")
    reserve = Decimal("50000")
    expected_qty = (q * reserve / (reserve + q)) / Decimal("2")
    assert fill.qty == expected_qty
    assert fill.fee_usd == Decimal("0.15")
    assert fill.slippage_usd == q - expected_qty * Decimal("2")
    assert fill.exec_price > Decimal("2")


def test_impact_grows_with_size_and_shrinks_with_liquidity():
    small = price_impact(Decimal("10"), Decimal("100000"))
    large = price_impact(Decimal("1000"), Decimal("100000"))
    deep = price_impact(Decimal("1000"), Decimal("5000000"))
    assert large > small
    assert large > deep


def test_round_trip_at_an_unchanged_mid_is_a_loss():
    cost = round_trip_cost(notional=Decimal("50"), mid=Decimal("2"), liquidity=Decimal("100000"),
                           fee_rate=Decimal("0.003"), gas_per_tx=Decimal("0.02"))
    assert cost > Decimal("0.04")  # both gas legs, plus fee and impact


def test_fee_rate_from_pool_name():
    snap = PoolSnapshot(
        source=DataSource.GECKOTERMINAL, chain=Chain.BASE, pool_address=POOL, dex_id=None,
        base_token=TokenRef(Chain.BASE, TOKEN), quote_token=None, observed_at=NOW, price_usd=Decimal("1"),
        price_native=None, liquidity_usd=Decimal("1"), fdv_usd=None, market_cap_usd=None, pool_created_at=None,
        raw={"attributes": {"name": "KNTQ / USDC 0.042% fee"}},
    )
    assert fee_rate_for_snapshot(snap) == Decimal("0.042") / Decimal(100)
    assert fee_rate_for_snapshot(PoolSnapshot(
        source=DataSource.DEXSCREENER, chain=Chain.BASE, pool_address=POOL, dex_id=None,
        base_token=TokenRef(Chain.BASE, TOKEN), quote_token=None, observed_at=NOW, price_usd=Decimal("1"),
        price_native=None, liquidity_usd=None, fdv_usd=None, market_cap_usd=None, pool_created_at=None,
    )) == Decimal("0.003")


def test_ledger_identity_and_immediate_unrealised_loss():
    buy = buy_fill(mid=Decimal("2"), notional=Decimal("50"), liquidity=Decimal("100000"),
                   fee_rate=Decimal("0.003"), gas_usd=Decimal("0.02"))
    ledger = Ledger(Decimal("1000"))
    ledger.open_position(OpenPos(
        key="p", chain=Chain.BASE, pool_address=POOL, token_address=TOKEN, strategy="momentum",
        qty=buy.qty, cost_basis=buy.quote_spent + buy.gas_usd, entry_exec=buy.exec_price, entry_mid=Decimal("2"),
        opened_at=NOW, stop=Decimal("1"), take_profit=Decimal("3"), max_hold_until=NOW, max_hold_bars=None,
        liquidity=Decimal("100000"), fee_rate=Decimal("0.003"), gas_per_tx=Decimal("0.02"),
        fees=buy.fee_usd, gas=buy.gas_usd, slippage=buy.slippage_usd, notional=buy.quote_spent,
    ), buy)
    assert ledger.realized == 0
    assert ledger.unrealized < 0
    assert ledger.equity == ledger.starting + ledger.net_pnl
    assert ledger.cash + ledger.deployed + ledger.unrealized == ledger.equity

    sell = sell_fill(qty=buy.qty, mid=Decimal("2.2"), liquidity=Decimal("100000"),
                     fee_rate=Decimal("0.003"), gas_usd=Decimal("0.02"))
    trade = ledger.close_position("p", sell, "take_profit", NOW)
    assert trade.net_pnl == realized_pnl(buy.quote_spent + buy.gas_usd, sell)
    assert ledger.positions == {}
    assert ledger.cash == ledger.starting + ledger.realized
    assert ledger.equity == ledger.cash
    assert ledger.net_pnl == ledger.realized


def _account() -> AccountSnapshot:
    return AccountSnapshot(Decimal("1000"), Decimal(0), 0, None, False, [])


def _request(**kw) -> EntryRequest:
    base = dict(chain=Chain.BASE, pool_address=POOL, token_address=TOKEN, strategy="momentum",
                mid_price=Decimal("2"), liquidity_usd=Decimal("100000"), fee_rate=Decimal("0.003"), now=NOW)
    base.update(kw)
    return EntryRequest(**base)


def test_default_limits_match_settings():
    assert limits_from_settings(Settings()) == RiskLimits()


def test_size_does_not_increase_after_losses():
    engine = RiskEngine()
    account = _account()
    first = engine.evaluate_entry(account, _request())
    account.consecutive_losses = 4
    account.day_realized_pnl_usd = Decimal("-10")
    second = engine.evaluate_entry(account, _request())
    assert first.approved and second.approved
    assert first.size_usd == second.size_usd == Decimal("50")
    assert engine.order_size() == Decimal("50")


def test_no_averaging_down():
    engine = RiskEngine()
    account = _account()
    account.open_positions = [OpenExposure(Chain.BASE, POOL, TOKEN, Decimal("50"))]
    decision = engine.evaluate_entry(account, _request(mid_price=Decimal("1")))
    assert not decision.approved
    assert "averaging down" in decision.reason


def test_daily_loss_kill_switch_and_the_boundary():
    engine = RiskEngine()
    account = _account()
    account.day_realized_pnl_usd = Decimal("-49.99")
    assert engine.evaluate_entry(account, _request()).approved
    account.day_realized_pnl_usd = Decimal("-50")
    decision = engine.evaluate_entry(account, _request())
    assert not decision.approved and account.kill_switch


def test_gas_reserve_is_not_spendable():
    engine = RiskEngine()
    account = _account()
    account.cash_usd = Decimal("55")
    assert not engine.evaluate_entry(account, _request()).approved
    account.cash_usd = Decimal("55.05")  # reserve 5 + size 50 + Base gas 0.05
    assert engine.evaluate_entry(account, _request()).approved


def test_concurrent_and_exposure_caps():
    engine = RiskEngine()
    full = _account()
    full.open_positions = [
        OpenExposure(Chain.BASE, f"0x{i:040x}", f"0x{(i + 1):040x}", Decimal("50")) for i in range(1, 6)
    ]
    assert not engine.evaluate_entry(full, _request()).approved

    wide = RiskEngine(RiskLimits(max_concurrent_positions=10))
    exposed = _account()
    exposed.open_positions = [
        OpenExposure(Chain.BASE, f"0x{i:040x}", f"0x{(i + 1):040x}", Decimal("50")) for i in range(1, 7)
    ]
    decision = wide.evaluate_entry(exposed, _request())
    assert not decision.approved
    assert "exposure" in decision.reason


def test_cooldown_after_two_losses_survives_a_later_win():
    engine = RiskEngine()
    account = _account()
    engine.register_close(Decimal("-1"), NOW, account)
    assert account.cooldown_until is None and account.consecutive_losses == 1
    engine.register_close(Decimal("-1"), NOW, account)
    assert account.consecutive_losses == 2
    assert account.cooldown_until == NOW + timedelta(seconds=900)
    blocked = engine.evaluate_entry(account, _request())
    assert not blocked.approved and "cooldown" in blocked.reason
    engine.register_close(Decimal("2"), NOW, account)
    assert account.consecutive_losses == 0
    assert account.cooldown_until == NOW + timedelta(seconds=900)
    later = engine.evaluate_entry(account, _request(now=NOW + timedelta(seconds=901)))
    assert later.approved


def test_costs_that_exceed_the_stop_are_rejected():
    engine = RiskEngine()
    decision = engine.evaluate_entry(_account(), _request(fee_rate=Decimal("0.05"), liquidity_usd=Decimal("1000000")))
    assert not decision.approved
    assert "stop" in decision.reason


def test_stops_are_measured_from_the_fill():
    stop, take = stops_from_exec(Decimal("1"), RiskLimits())
    assert stop == Decimal("0.92")
    assert take == Decimal("1.15")
    decision = RiskEngine().evaluate_entry(_account(), _request())
    assert decision.stop_loss_pct == Decimal("0.08")
    assert decision.take_profit_pct == Decimal("0.15")
    assert decision.max_hold_until == NOW + timedelta(minutes=240)


def test_slippage_cap_rejects_a_thin_pool():
    decision = RiskEngine().evaluate_entry(_account(), _request(liquidity_usd=Decimal("1000")))
    assert not decision.approved
    assert "slippage" in decision.reason
