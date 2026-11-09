"""Defensive parsing helpers shared by providers."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def dec(value: Any) -> Decimal | None:
    """Parse to Decimal. None, empty, NaN and infinities become None (unknown)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ts_ms(value: Any) -> datetime | None:
    v = int_or_none(value)
    return None if v is None or v <= 0 else datetime.fromtimestamp(v / 1000, tz=timezone.utc)


def ts_s(value: Any) -> datetime | None:
    v = int_or_none(value)
    return None if v is None or v <= 0 else datetime.fromtimestamp(v, tz=timezone.utc)


def iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
