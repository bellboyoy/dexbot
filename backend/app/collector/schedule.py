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


def clock_jumped(previous: datetime | None, now: datetime, jump_s: float) -> bool:
    """True when wall time leapt forward, which is what a host suspend looks like."""
    if previous is None:
        return False
    return (now - previous).total_seconds() >= jump_s


def catch_up_limit(jumped: bool, normal: int, catch_up: int = 50) -> int:
    """After a gap, close more overdue watches in one cycle. The extra rows are missed slots."""
    return catch_up if jumped else normal


def fatal_stall(consecutive: int, limit: int) -> bool:
    """A cycle that keeps hitting its deadline should exit so a watchdog can restart it."""
    return consecutive >= max(1, limit)


def is_usable(observed: int, elapsed: int, *, min_observed: int = 8, min_coverage: float = 0.5) -> bool:
    """A pool counts toward the research gate only when enough elapsed slots were actually seen.

    Missed slots and unsupported pool ids are not observations. Coverage is observed / elapsed.
    """
    if elapsed < min_observed or observed < min_observed or elapsed <= 0:
        return False
    return (observed / elapsed) >= min_coverage


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
