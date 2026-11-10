import asyncio

import httpx
import pytest
import respx

from app.marketdata.errors import InvalidResponse, ProviderUnavailable, RateLimited
from app.marketdata.http import CircuitBreaker, JsonHttpClient, MemoryTTLCache, RateLimiter

BASE = "https://example.test"


def make_client(**kw):
    return JsonHttpClient("test", BASE, limiters={"default": RateLimiter(10_000, 1, burst=1000)},
                          backoff_base_s=0.001, **kw)


async def test_request_timeout_is_bounded():
    client = make_client(timeout_s=0.05, max_retries=0)

    async def hang(*args, **kwargs):
        await asyncio.sleep(2)

    client._client.get = hang
    with pytest.raises(ProviderUnavailable):
        await client.get_json("/x")
    await client.aclose()


async def test_rate_limiter_spaces_calls():
    rl = RateLimiter(rate=20, per=1.0, burst=1)   # one call per 50ms after the first
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(4):
        await rl.acquire()
    assert loop.time() - start >= 0.14


async def test_rate_limiter_penalty_blocks():
    rl = RateLimiter(rate=1000, per=1.0, burst=10)
    rl.penalize(0.1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await rl.acquire()
    assert loop.time() - start >= 0.09


def test_rate_limiter_rejects_bad_config():
    with pytest.raises(ValueError):
        RateLimiter(0)


async def test_ttl_cache_expiry(clock):
    c = MemoryTTLCache(clock=clock)
    await c.set("k", {"a": 1}, ttl_s=5)
    assert await c.get("k") == {"a": 1}
    clock.advance(5)
    assert await c.get("k") is None


async def test_ttl_cache_evicts_oldest(clock):
    c = MemoryTTLCache(max_entries=2, clock=clock)
    for k in "abc":
        await c.set(k, k, 10)
    assert await c.get("a") is None and await c.get("c") == "c"


def test_half_open_allows_a_single_probe(clock):
    breaker = CircuitBreaker(threshold=1, cooldown_s=30, clock=clock)
    breaker.record_failure()
    assert not breaker.allow()
    clock.advance(30)
    assert breaker.state == "half_open"
    assert breaker.allow() is True
    assert breaker.allow() is False
    breaker.release_probe()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"


def test_circuit_breaker_cycle(clock):
    b = CircuitBreaker(threshold=2, cooldown_s=30, clock=clock)
    b.record_failure()
    assert b.allow()
    b.record_failure()
    assert b.state == "open" and not b.allow()
    clock.advance(30)
    assert b.state == "half_open" and b.allow()
    b.record_failure()                       # failed probe re-opens immediately
    assert b.state == "open"
    clock.advance(30)
    b.record_success()
    assert b.state == "closed"


@respx.mock
async def test_get_json_caches():
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, json={"ok": 1}))
    c = make_client()
    assert await c.get_json("/x", cache_ttl_s=60) == {"ok": 1}
    assert await c.get_json("/x", cache_ttl_s=60) == {"ok": 1}
    assert route.call_count == 1 and c.status().cache_hits == 1
    await c.aclose()


@respx.mock
async def test_retries_5xx_then_succeeds():
    route = respx.get(f"{BASE}/x").mock(side_effect=[httpx.Response(502), httpx.Response(200, json=[1])])
    c = make_client()
    assert await c.get_json("/x") == [1]
    assert route.call_count == 2
    await c.aclose()


@respx.mock
async def test_429_respects_retry_after_and_raises_rate_limited():
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(429, headers={"Retry-After": "0.01"}))
    c = make_client(max_retries=2)
    with pytest.raises(RateLimited):
        await c.get_json("/x")
    assert c.status().rate_limited == 3
    await c.aclose()


@respx.mock
async def test_timeouts_become_unavailable_and_open_breaker(clock):
    respx.get(f"{BASE}/x").mock(side_effect=httpx.ConnectTimeout("boom"))
    c = make_client(max_retries=0, breaker=CircuitBreaker(threshold=2, cooldown_s=60, clock=clock))
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            await c.get_json("/x")
    with pytest.raises(ProviderUnavailable, match="circuit breaker open"):
        await c.get_json("/x")
    await c.aclose()


@respx.mock
async def test_404_allowed_returns_none_and_4xx_raises():
    respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/bad").mock(return_value=httpx.Response(400, text="bad"))
    c = make_client()
    assert await c.get_json("/missing", allow_404=True) is None
    with pytest.raises(InvalidResponse):
        await c.get_json("/bad")
    assert c.breaker.state == "closed"
    await c.aclose()


@respx.mock
async def test_non_json_body():
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, text="<html>"))
    c = make_client()
    with pytest.raises(InvalidResponse):
        await c.get_json("/x")
    await c.aclose()


def test_client_requires_default_limiter():
    with pytest.raises(ValueError):
        JsonHttpClient("t", BASE, limiters={"other": RateLimiter(1)})
