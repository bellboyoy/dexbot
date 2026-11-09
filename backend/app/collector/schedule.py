"""When to observe a launch. Slots that were missed are recorded as missed, not backfilled."""
from __future__ import annotations

from datetime import datetime, timedelta

# Early life, then hourly from 5 hours through 24 hours.
EARLY_MINUTES = (1, 5, 15, 30, 60, 240)
HOURLY_FROM = 300
HOURLY_UNTIL = 24 * 60
BUYER_AGES = frozenset({15, 60, 240})
SCREEN_AGES = frozenset({1, 60, 240})
# A row is about a kilobyte once indexes and the small screen document are counted.
BYTES_PER_SNAPSHOT = 1200


def snapshot_ages() -> tuple[int, ...]:
    hourly = tuple(range(HOURLY_FROM, HOURLY_UNTIL + 1, 60))
    return EARLY_MINUTES + hourly


def due_at(launch: datetime, age_minutes: int) -> datetime:
    return launch + timedelta(minutes=age_minutes)


def tolerance_seconds(age_minutes: int) -> int:
    if age_minutes <= 1:
        return 45
    if age_minutes <= 5:
        return 90
    return 180


def classify(launch: datetime, age_minutes: int, now: datetime) -> str:
    """`wait`, `due`, or `missed`."""
    when = due_at(launch, age_minutes)
    if now < when:
        return "wait"
    if now <= when + timedelta(seconds=tolerance_seconds(age_minutes)):
        return "due"
    return "missed"


def partition(launch: datetime, now: datetime, done: set[int] | None = None) -> tuple[list[int], int | None]:
    """Missed ages that should be closed, and the next age that is due or still in the future."""
    finished = done or set()
    missed: list[int] = []
    for age in snapshot_ages():
        if age in finished:
            continue
        kind = classify(launch, age, now)
        if kind == "missed":
            missed.append(age)
            continue
        return missed, age
    return missed, None


def daily_snapshot_rows(new_pools_per_day: int) -> int:
    return new_pools_per_day * len(snapshot_ages())


def daily_megabytes(new_pools_per_day: int) -> float:
    return daily_snapshot_rows(new_pools_per_day) * BYTES_PER_SNAPSHOT / 1_000_000


def asof_liquidity(points: list[tuple[datetime, object]], at: datetime):
    """Last known liquidity at or before `at`. Later snapshots are invisible."""
    chosen = None
    chosen_at = None
    for observed, value in points:
        if value is None or observed > at:
            continue
        if chosen_at is None or observed >= chosen_at:
            chosen = value
            chosen_at = observed
    return chosen
