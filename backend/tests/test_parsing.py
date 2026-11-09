from decimal import Decimal

from app.marketdata.parsing import dec, iso, ts_ms


def test_dec_unknowns_are_none():
    for v in (None, "", "NaN", "inf", "abc", True):
        assert dec(v) is None


def test_dec_keeps_precision():
    s = "0.00000637351563433618879130895607529103570875936975554177369676278572"
    assert dec(s) == Decimal(s)
    assert dec(0.1) == Decimal("0.1")


def test_timestamps():
    assert ts_ms(1688106058000).year == 2023
    assert ts_ms(None) is None and ts_ms(0) is None
    assert iso("2026-11-09T20:16:01Z").tzinfo is not None
    assert iso("not a date") is None
