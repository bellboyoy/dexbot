"""Resilient HTTP access for market data providers.

Provides a token-bucket rate limiter, a TTL response cache, a circuit breaker and a
JSON client that combines them with retries, exponential backoff and Retry-After
handling. The cache interface is small so a Redis backend can replace the in-memory
one without touching providers.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from app.logging import get_logger
from app.marketdata.errors import InvalidResponse, ProviderUnavailable, RateLimited

log = get_logger(__name__)

Clock = Callable[[], float]


class RateLimiter:
    """Async token bucket: `rate` tokens per `per` seconds, burst up to `burst`."""

    def __init__(self, rate: int, per: float = 60.0, burst: int | None = None, clock: Clock = time.monotonic):
        if rate <= 0 or per <= 0:
            raise ValueError("rate and per must be positive")
        self.capacity = float(burst if burst is not None else max(1, min(rate, 5)))
        self.refill_per_s = rate / per
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_s)
        self._last = now

    def penalize(self, seconds: float) -> None:
        """Block all callers for `seconds` (used after a 429).

        Called from the request path without awaiting, so it does not interleave with the
        locked section of `acquire` on the asyncio thread.
        """
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)
        self._tokens = 0.0

    async def acquire(self) -> None:
        # Sleep outside the lock. Holding it across sleep made every other caller wait
        # out the first caller's full delay, and a 429 penalty could not be observed
        # until that sleep finished.
        while True:
            async with self._lock:
                now = self._clock()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    self._refill()
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    wait = (1 - self._tokens) / self.refill_per_s
            await asyncio.sleep(wait)


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_s: float) -> None: ...


class MemoryTTLCache:
    def __init__(self, max_entries: int = 5000, clock: Clock = time.monotonic):
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max = max_entries
        self._clock = clock

    async def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if self._clock() >= expires:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: Any, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        self._data[key] = (self._clock() + ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; half-opens after `cooldown_s`."""

    def __init__(self, threshold: int = 5, cooldown_s: float = 60.0, clock: Clock = time.monotonic):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_inflight = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "open":
            return False
        if state == "half_open":
            # One probe at a time. A scan fan-out must not dump the full rate onto a
            # provider that is only possibly back.
            if self._probe_inflight:
                return False
            self._probe_inflight = True
        return True

    def release_probe(self) -> None:
        """A 4xx is not a recovery signal and must not leave the probe stuck."""
        self._probe_inflight = False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_inflight = False

    def record_failure(self) -> None:
        half_open = self.state == "half_open"
        self._probe_inflight = False
        self._failures += 1
        if half_open or self._failures >= self.threshold:
            self._opened_at = self._clock()


@dataclass
class ProviderStatus:
    name: str
    breaker_state: str
    last_error: str | None
    last_success_at: float | None
    requests: int
    cache_hits: int
    rate_limited: int


class JsonHttpClient:
    """GET-JSON client with cache, limiter(s), breaker and retries for one provider."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        limiters: dict[str, RateLimiter],
        cache: Cache | None = None,
        breaker: CircuitBreaker | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        rate_limit_penalty_s: float = 5.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if "default" not in limiters:
            raise ValueError("limiters must include a 'default' entry")
        self.provider = provider
        self.limiters = limiters
        self.cache = cache or MemoryTTLCache()
        self.breaker = breaker or CircuitBreaker()
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.rate_limit_penalty_s = rate_limit_penalty_s
        self._timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            headers={"Accept": "application/json", "User-Agent": "dexbot/0.1", **(headers or {})},
            transport=transport,
        )
        self._last_error: str | None = None
        self._last_success: float | None = None
        self._requests = 0
        self._cache_hits = 0
        self._rate_limited = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.provider, self.breaker.state, self._last_error, self._last_success,
                              self._requests, self._cache_hits, self._rate_limited)

    async def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cache_ttl_s: float = 0.0,
        limiter: str = "default",
        allow_404: bool = False,
    ) -> Any | None:
        key = f"{self.provider}:{path}?{sorted((params or {}).items())}"
        if cache_ttl_s > 0:
            cached = await self.cache.get(key)
            if cached is not None:
                self._cache_hits += 1
                return cached

        if not self.breaker.allow():
            raise ProviderUnavailable(self.provider, "circuit breaker open")

        bucket = self.limiters.get(limiter, self.limiters["default"])
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await bucket.acquire()
            self._requests += 1
            try:
                resp = await asyncio.wait_for(self._client.get(path, params=params), timeout=self._timeout_s)
            except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as exc:
                last_exc = exc
                self._last_error = f"{type(exc).__name__}: {exc}"
                await self._backoff(attempt)
                continue

            if resp.status_code == 429:
                self._rate_limited += 1
                wait = self._retry_after(resp) or self.rate_limit_penalty_s * (2 ** attempt)
                bucket.penalize(wait)
                last_exc = RateLimited(self.provider, f"429 on {path}")
                self._last_error = "429 rate limited"
                log.warning("provider_rate_limited", provider=self.provider, path=path, wait_s=wait)
                continue
            if resp.status_code == 404 and allow_404:
                self.breaker.record_success()
                return None
            if resp.status_code >= 500:
                last_exc = ProviderUnavailable(self.provider, f"HTTP {resp.status_code} on {path}")
                self._last_error = f"HTTP {resp.status_code}"
                await self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                # Client errors are not retried and do not trip the breaker.
                self._last_error = f"HTTP {resp.status_code}"
                self.breaker.release_probe()
                raise InvalidResponse(self.provider, f"HTTP {resp.status_code} on {path}: {resp.text[:200]}")

            try:
                data = resp.json()
            except ValueError as exc:
                self.breaker.record_failure()
                raise InvalidResponse(self.provider, f"non-JSON body on {path}") from exc
            self.breaker.record_success()
            self._last_success = time.time()
            self._last_error = None
            if cache_ttl_s > 0:
                await self.cache.set(key, data, cache_ttl_s)
            return data

        self.breaker.record_failure()
        log.error("provider_request_failed", provider=self.provider, path=path, error=str(last_exc))
        if isinstance(last_exc, RateLimited):
            raise last_exc
        raise ProviderUnavailable(self.provider, f"request failed after retries: {last_exc}")

    async def _backoff(self, attempt: int) -> None:
        if attempt < self.max_retries:
            delay = self.backoff_base_s * (2 ** attempt)
            await asyncio.sleep(delay + random.uniform(0, delay / 4))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None
