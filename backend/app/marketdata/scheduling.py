"""Priority gate for scarce provider capacity (keyless GeckoTerminal).

The rate limiter decides *when* a request may go out; this gate decides *which* waiting
job goes next. Lower priority number = served first; equal priorities are FIFO.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
from contextlib import asynccontextmanager
from enum import IntEnum


class Priority(IntEnum):
    DISCOVERY = 0
    POSITION = 1        # reserved for open-position monitoring (Phase 6)
    BUYERS = 3          # unique-buyer counts / second-source cross-check
    BACKFILL = 5


class PriorityGate:
    def __init__(self) -> None:
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._busy = False
        self._seq = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())

    async def acquire(self, priority: int) -> None:
        if not self._busy and not self._heap:
            self._busy = True
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()          # we were granted the slot just as we got cancelled
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
                return
        self._busy = False

    @asynccontextmanager
    async def slot(self, priority: int):
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()
