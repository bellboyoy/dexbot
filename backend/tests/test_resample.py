from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.candles import resample
from app.domain.market import Candle, Timeframe
from tests.factories import NOW


def c(minute, o, h, l, cl, v):
    return Candle(NOW + timedelta(minutes=minute), Timeframe.M1, Decimal(o), Decimal(h), Decimal(l),
                  Decimal(cl), Decimal(v), True)


def test_resample_to_5m_with_gap_and_edges():
    # NOW is aligned to the hour. Minutes 0-9 with minute 3 missing (no trades), minute 10 partial bucket.
    data = [c(m, 1 + m, 2 + m, 0.5 + m, 1.5 + m, 10) for m in range(10) if m != 3] + [c(10, 5, 6, 4, 5, 1)]
    out = resample(data, Timeframe.M5)
    assert [x.open_time for x in out] == [NOW, NOW + timedelta(minutes=5)]     # 10-14 bucket not fully covered
    first = out[0]
    assert first.open == 1 and first.close == Decimal("5.5") and first.high == 6 and first.low == Decimal("0.5")
    assert first.volume_usd == 40                                              # 4 candles, missing minute adds nothing


def test_resample_rejects_bad_targets():
    with pytest.raises(ValueError):
        resample([c(0, 1, 1, 1, 1, 1)], Timeframe.M1)
    assert resample([], Timeframe.H1) == []
