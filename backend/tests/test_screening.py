"""Screening decisions. Parser fixtures follow live RugCheck and GoPlus responses from 2026-11-09."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.chains import Chain
from app.domain.market import DataSource, PoolSnapshot, TokenRef, WindowStats
from app.marketdata.errors import ProviderUnavailable
from app.screening.engine import ScreeningEngine
from app.screening.filters import ScreenThresholds, market_checks
from app.screening.models import CheckStatus, decide
from app.screening.providers import parse_goplus_evm, parse_rugcheck

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN = "0x" + "11" * 20
POOL = "0x" + "22" * 20
WSOL = "So11111111111111111111111111111111111111112"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def snap(**kw) -> PoolSnapshot:
    base = dict(
        source=DataSource.DEXSCREENER, chain=Chain.BASE, pool_address=POOL, dex_id="uniswap",
        base_token=TokenRef(Chain.BASE, TOKEN, "TOK"),
        quote_token=TokenRef(Chain.BASE, USDC, "USDC"), observed_at=NOW, price_usd=Decimal("1.5"),
        price_native=Decimal("1"), liquidity_usd=Decimal("50000"), fdv_usd=None, market_cap_usd=None,
        pool_created_at=NOW - timedelta(days=10),
        windows={"h1": WindowStats(buys=20, sells=15, volume_usd=Decimal("8000"))},
    )
    base.update(kw)
    return PoolSnapshot(**base)


def test_healthy_pool_passes_market_filters():
    decision = decide(market_checks(snap(), ScreenThresholds(), now=NOW))
    assert decision.allowed, decision.reason


def test_missing_liquidity_is_unknown_and_blocks():
    decision = decide(market_checks(snap(liquidity_usd=None), ScreenThresholds(), now=NOW))
    assert not decision.allowed
    assert any(c.name == "min_liquidity" and c.status is CheckStatus.UNKNOWN for c in decision.checks)


def test_low_liquidity_volume_age_and_no_sells_fail():
    thresholds = ScreenThresholds()
    assert not decide(market_checks(snap(liquidity_usd=Decimal("1000")), thresholds, now=NOW)).allowed
    assert not decide(market_checks(
        snap(windows={"h1": WindowStats(buys=10, sells=8, volume_usd=Decimal("100"))}), thresholds, now=NOW,
    )).allowed
    young = snap(pool_created_at=NOW - timedelta(minutes=5))
    assert not decide(market_checks(young, thresholds, now=NOW)).allowed
    no_sells = snap(windows={"h1": WindowStats(buys=12, sells=0, volume_usd=Decimal("9000"))})
    decision = decide(market_checks(no_sells, thresholds, now=NOW))
    assert not decision.allowed and any(c.name == "recent_sells" for c in decision.checks)


def test_bad_quote_and_stable_base_fail():
    bad_quote = snap(quote_token=TokenRef(Chain.BASE, "0x" + "33" * 20, "SCAM"))
    assert not decide(market_checks(bad_quote, ScreenThresholds(), now=NOW)).allowed
    stable = snap(base_token=TokenRef(Chain.BASE, USDC, "USDC"))
    assert not decide(market_checks(stable, ScreenThresholds(), now=NOW)).allowed


def _goplus(fields: dict) -> dict:
    return {"code": 1, "message": "OK", "result": {TOKEN: fields}}


def test_goplus_honeypot_tax_and_thin_record():
    thresholds = ScreenThresholds()
    clean = {
        "is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_mintable": "0",
        "cannot_sell_all": "0", "cannot_buy": "0", "transfer_pausable": "0",
        "lp_holders": [{"percent": "0.83", "is_locked": 0}],
    }
    old = decide(parse_goplus_evm(_goplus(clean), TOKEN, young=False, thresholds=thresholds))
    assert old.allowed, old.reason
    young = decide(parse_goplus_evm(_goplus(clean), TOKEN, young=True, thresholds=thresholds))
    assert not young.allowed and any(c.name == "lp_lock" for c in young.checks)
    poisoned = dict(clean, is_honeypot="1")
    assert not decide(parse_goplus_evm(_goplus(poisoned), TOKEN, young=False, thresholds=thresholds)).allowed
    taxed = dict(clean, sell_tax="0.2")
    assert not decide(parse_goplus_evm(_goplus(taxed), TOKEN, young=False, thresholds=thresholds)).allowed
    # Live shape of a minutes-old Base token: empty taxes and no honeypot flag.
    thin = {"buy_tax": "", "sell_tax": "", "cannot_buy": "0", "is_in_dex": "0"}
    blocked = decide(parse_goplus_evm(_goplus(thin), TOKEN, young=True, thresholds=thresholds))
    assert not blocked.allowed
    assert any(c.status is CheckStatus.UNKNOWN and c.critical for c in blocked.checks)


def test_rugcheck_authorities_and_warnings():
    thresholds = ScreenThresholds()
    clean = {
        "mint": BONK, "mintAuthority": None, "freezeAuthority": None, "rugged": False, "risks": [],
        "markets": [{"lp": {"lpLockedPct": 99, "baseUSD": "1000", "quoteUSD": "1000"}}],
        "transferFee": None,
    }
    assert decide(parse_rugcheck(clean, young=True, thresholds=thresholds)).allowed
    minted = dict(clean, mintAuthority="still-there")
    assert not decide(parse_rugcheck(minted, young=True, thresholds=thresholds)).allowed
    frozen = dict(clean, freezeAuthority="still-there")
    assert not decide(parse_rugcheck(frozen, young=False, thresholds=thresholds)).allowed
    danger = dict(clean, risks=[{"name": "Honeypot", "level": "danger", "description": "cannot sell"}])
    assert not decide(parse_rugcheck(danger, young=False, thresholds=thresholds)).allowed
    warned = dict(clean, risks=[{"name": "Mutable metadata", "level": "warn", "description": "mutable"}])
    decision = decide(parse_rugcheck(warned, young=False, thresholds=thresholds))
    assert decision.allowed
    assert any(c.critical is False and c.status is CheckStatus.FAIL for c in decision.checks)
    unlocked = dict(clean, markets=[{"lp": {"lpLockedPct": 0, "baseUSD": "5000", "quoteUSD": "5000"}}])
    assert not decide(parse_rugcheck(unlocked, young=True, thresholds=thresholds)).allowed
    assert decide(parse_rugcheck(unlocked, young=False, thresholds=thresholds)).allowed


class _Down:
    async def rugcheck_report(self, mint):
        raise ProviderUnavailable("rugcheck", "timeout")

    async def goplus_solana(self, mint):
        raise ProviderUnavailable("goplus", "timeout")

    async def goplus_evm(self, chain, address):
        raise ProviderUnavailable("goplus", "timeout")

    async def aclose(self):
        pass


async def test_security_outage_blocks_instead_of_passing():
    pool = snap(
        chain=Chain.SOLANA, pool_address="Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",
        base_token=TokenRef(Chain.SOLANA, BONK, "BONK"),
        quote_token=TokenRef(Chain.SOLANA, WSOL, "SOL"),
    )
    decision = await ScreeningEngine(_Down(), ScreenThresholds()).screen(pool, now=NOW)
    assert not decision.allowed
    assert any(c.name == "provider_unavailable" and c.status is CheckStatus.UNKNOWN for c in decision.checks)
