from datetime import timedelta

from app.collector.worker import CollectorConfig, next_status
from tests.factories import NOW, snap

CFG = CollectorConfig()


def test_healthy_pool_active():
    assert next_status("inactive", NOW, snap(), 0, NOW, CFG) == "active"


def test_thin_or_quiet_pool_inactive():
    assert next_status("active", NOW, snap(liquidity="3000"), 0, NOW, CFG) == "inactive"
    assert next_status("active", NOW, snap(h1=(0, 0)), 0, NOW, CFG) == "inactive"
    assert next_status("active", NOW, snap(liquidity=None), 0, NOW, CFG) == "inactive"


def test_rugged_pool_dead_but_can_recover():
    assert next_status("active", NOW, snap(liquidity="200"), 0, NOW, CFG) == "dead"
    assert next_status("dead", NOW, snap(), 0, NOW, CFG) == "active"


def test_misses_kill_only_after_limit():
    assert next_status("active", NOW, None, CFG.miss_limit - 1, NOW, CFG) == "active"
    assert next_status("active", NOW, None, CFG.miss_limit, NOW, CFG) == "dead"


def test_retire_after_long_inactivity():
    since = NOW - CFG.retire_after
    assert next_status("dead", since, snap(liquidity="200"), 0, NOW, CFG) == "retired"
    assert next_status("inactive", since, snap(liquidity="3000"), 0, NOW, CFG) == "retired"
    assert next_status("inactive", NOW - timedelta(hours=1), snap(liquidity="3000"), 0, NOW, CFG) == "inactive"
    assert next_status("retired", NOW, snap(), 0, NOW, CFG) == "retired"
