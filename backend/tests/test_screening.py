from datetime import timedelta

import pytest

from app.discovery.screening import Profile, ScreeningConfig, screen
from app.domain.chains import Chain
from tests.factories import NOW, USDC_SOL, sol_addr, snap


def test_new_token_passes():
    r = screen(snap(), NOW)
    assert r.passed and r.profile is Profile.NEW and r.rejections == []


def test_established_profile_used_for_old_pools():
    r = screen(snap(age=timedelta(days=30), liquidity="2000000", quote=USDC_SOL, quote_symbol="USDC"), NOW)
    assert r.passed and r.profile is Profile.ESTABLISHED


@pytest.mark.parametrize("kwargs, reason", [
    (dict(liquidity="1000"), "low_liquidity"),
    (dict(liquidity=None), "unknown_liquidity"),
    (dict(h1=(5, 3)), "low_activity"),
    (dict(h1=None), "unknown_activity"),
    (dict(h1=(40, 0)), "no_sells"),
    (dict(age=timedelta(minutes=2)), "too_young"),
    (dict(quote=sol_addr(99), quote_symbol="MEME"), "quote_not_exitable"),
])
def test_new_token_rejections(kwargs, reason):
    r = screen(snap(**kwargs), NOW)
    assert not r.passed and reason in r.rejections


def test_unknown_age_rejected_without_profile():
    r = screen(snap(age=None), NOW)
    assert not r.passed and r.profile is None and "unknown_pool_age" in r.rejections


@pytest.mark.parametrize("kwargs, reason", [
    (dict(liquidity="50000"), "low_liquidity"),
    (dict(vol24="1000"), "low_volume"),
    (dict(vol24=None), "unknown_volume"),
    (dict(h24=(10, 10)), "low_activity"),
])
def test_established_rejections(kwargs, reason):
    base = dict(age=timedelta(days=10), liquidity="500000")
    base.update(kwargs)
    r = screen(snap(**base), NOW)
    assert r.profile is Profile.ESTABLISHED and not r.passed and reason in r.rejections


def test_only_verified_quotes_count():
    cfg = ScreeningConfig(verified_quotes={Chain.SOLANA: {USDC_SOL}})      # WSOL failed verification
    assert "quote_not_exitable" in screen(snap(), NOW, cfg).rejections
    assert screen(snap(quote=USDC_SOL, quote_symbol="USDC"), NOW, cfg).passed


def test_metrics_recorded():
    m = screen(snap(), NOW).metrics
    assert m["h1_txns"] == 50 and m["pool_age_min"] == 120.0 and m["quote"] == "SOL"
