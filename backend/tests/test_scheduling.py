import asyncio

import pytest

from app.marketdata.scheduling import Priority, PriorityGate


async def test_priority_order_and_fifo():
    gate = PriorityGate()
    order = []
    await gate.acquire(0)                   # hold the gate so the rest queue up

    async def job(name, prio):
        async with gate.slot(prio):
            order.append(name)

    tasks = [asyncio.create_task(job(n, p)) for n, p in
             [("backfill1", Priority.BACKFILL), ("disc1", Priority.DISCOVERY),
              ("backfill2", Priority.BACKFILL), ("disc2", Priority.DISCOVERY)]]
    await asyncio.sleep(0)
    assert gate.waiting == 4
    gate.release()
    await asyncio.gather(*tasks)
    assert order == ["disc1", "disc2", "backfill1", "backfill2"]
    assert gate.waiting == 0 and not gate._busy


async def test_cancelled_waiter_does_not_block():
    gate = PriorityGate()
    await gate.acquire(0)
    waiter = asyncio.create_task(gate.acquire(1))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.release()
    await asyncio.wait_for(gate.acquire(1), 1)      # gate is free again
    gate.release()


async def test_exception_inside_slot_releases():
    gate = PriorityGate()
    with pytest.raises(RuntimeError):
        async with gate.slot(0):
            raise RuntimeError
    await asyncio.wait_for(gate.acquire(0), 1)
