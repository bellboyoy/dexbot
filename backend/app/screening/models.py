"""Security and market-filter results. Unknown is not a pass."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    critical: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ScreenDecision:
    allowed: bool
    reason: str
    checks: tuple[Check, ...]


def decide(checks: list[Check]) -> ScreenDecision:
    """A critical FAIL or a critical UNKNOWN blocks. A non-critical FAIL is a warning."""
    blocking = [c for c in checks if c.critical and c.status is not CheckStatus.PASS]
    if not blocking:
        return ScreenDecision(True, "passed", tuple(checks))
    reason = "; ".join(f"{c.name}={c.status.value}: {c.detail}" for c in blocking[:8])
    return ScreenDecision(False, reason, tuple(checks))
