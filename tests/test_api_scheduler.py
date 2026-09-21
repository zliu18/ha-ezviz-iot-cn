"""Tests for the bounded per-hub request scheduler."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.ezviz_iot_cn.api.errors import (
    EzvizQueueFullError,
    EzvizSchedulerClosedError,
)
from custom_components.ezviz_iot_cn.api.scheduler import (
    HubRequestScheduler,
    RequestPriority,
)


async def _wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Condition was not reached")


@pytest.mark.asyncio
async def test_scheduler_never_exceeds_worker_limit() -> None:
    """A hub can never create more simultaneous network jobs than workers."""
    scheduler = HubRequestScheduler(worker_count=2, max_queue_size=8)
    await scheduler.async_start()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def operation(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await release.wait()
        active -= 1
        return value

    callers = [
        asyncio.create_task(
            scheduler.async_submit(lambda value=value: operation(value))
        )
        for value in range(4)
    ]
    await _wait_until(lambda: active == 2)
    release.set()

    assert await asyncio.gather(*callers) == [0, 1, 2, 3]
    assert maximum_active == 2
    assert scheduler.stats.high_watermark <= 2
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_priority_orders_queued_jobs_without_preempting_running_job() -> None:
    """User controls run before queued maintenance after the active job finishes."""
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    await scheduler.async_start()
    release = asyncio.Event()
    order: list[str] = []

    async def blocker() -> str:
        order.append("running")
        await release.wait()
        return "running"

    async def record(name: str) -> str:
        order.append(name)
        return name

    running = asyncio.create_task(
        scheduler.async_submit(blocker, priority=RequestPriority.STATE)
    )
    await _wait_until(lambda: order == ["running"])
    metadata = asyncio.create_task(
        scheduler.async_submit(
            lambda: record("metadata"), priority=RequestPriority.METADATA
        )
    )
    control = asyncio.create_task(
        scheduler.async_submit(
            lambda: record("control"), priority=RequestPriority.USER_CONTROL
        )
    )
    await _wait_until(lambda: scheduler.stats.queue_size == 2)
    release.set()

    assert await asyncio.gather(running, metadata, control) == [
        "running",
        "metadata",
        "control",
    ]
    assert order == ["running", "control", "metadata"]
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_full_queue_rejects_new_job_immediately() -> None:
    """Network degradation cannot grow an unbounded local backlog."""
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=1)
    await scheduler.async_start()
    release = asyncio.Event()

    async def blocked() -> None:
        await release.wait()

    running = asyncio.create_task(scheduler.async_submit(blocked))
    await _wait_until(lambda: scheduler.stats.running_count == 1)
    queued = asyncio.create_task(scheduler.async_submit(blocked))
    await _wait_until(lambda: scheduler.stats.queue_size == 1)

    with pytest.raises(EzvizQueueFullError):
        await scheduler.async_submit(blocked)

    release.set()
    await asyncio.gather(running, queued)
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_singleflight_callers_share_one_operation() -> None:
    """Matching coordinator refreshes reuse one queued or running request."""
    scheduler = HubRequestScheduler(worker_count=2, max_queue_size=4)
    await scheduler.async_start()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "snapshot"

    first = asyncio.create_task(scheduler.async_submit_singleflight("state", operation))
    second = asyncio.create_task(
        scheduler.async_submit_singleflight("state", operation)
    )
    await _wait_until(lambda: scheduler.stats.singleflight_count == 1)
    release.set()

    assert await asyncio.gather(first, second) == ["snapshot", "snapshot"]
    assert calls == 1
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_operation_exception_propagates_and_worker_survives() -> None:
    """One failed cloud request must not terminate its worker."""
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    await scheduler.async_start()

    async def broken() -> None:
        raise RuntimeError("synthetic failure")

    async def healthy() -> str:
        return "healthy"

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await scheduler.async_submit(broken)
    assert await scheduler.async_submit(healthy) == "healthy"
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_close_is_finite_and_settles_running_and_queued_callers() -> None:
    """Unload cannot leave callers or worker tasks alive forever."""
    scheduler = HubRequestScheduler(
        worker_count=1,
        max_queue_size=2,
        close_timeout=0.01,
    )
    await scheduler.async_start()
    never = asyncio.Event()

    async def blocked() -> None:
        await never.wait()

    running = asyncio.create_task(scheduler.async_submit(blocked))
    await _wait_until(lambda: scheduler.stats.running_count == 1)
    queued = asyncio.create_task(scheduler.async_submit(blocked))
    await _wait_until(lambda: scheduler.stats.queue_size == 1)

    await scheduler.async_close()

    for caller in (running, queued):
        with pytest.raises(EzvizSchedulerClosedError):
            await caller
    assert scheduler.stats.closed is True
    assert scheduler.stats.worker_count == 0
    assert scheduler.stats.queue_size == 0
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_closed_scheduler_rejects_new_jobs() -> None:
    """No background activity can restart after config-entry unload."""
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=1)
    await scheduler.async_start()
    await scheduler.async_close()

    async def operation() -> None:
        return None

    with pytest.raises(EzvizSchedulerClosedError):
        await scheduler.async_submit(operation)
