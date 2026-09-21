"""Bounded priority scheduler for one EZVIZ Open Platform hub."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from itertools import count
from typing import Any

from .errors import EzvizQueueFullError, EzvizSchedulerClosedError


class RequestPriority(IntEnum):
    """Smaller values run before larger values once a worker is available."""

    USER_CONTROL = 0
    AUTHENTICATION = 10
    PUSH_EVENT = 20
    STATE = 30
    ALARM = 40
    METADATA = 50
    SCHEDULED_CAPTURE = 60


@dataclass(frozen=True, slots=True)
class SchedulerStats:
    """Bounded diagnostic snapshot without request contents."""

    queue_size: int
    queue_high_watermark: int
    running_count: int
    high_watermark: int
    worker_count: int
    singleflight_count: int
    closed: bool


@dataclass(slots=True)
class _Job[T]:
    operation: Callable[[], Awaitable[T]]
    future: asyncio.Future[T]
    singleflight_key: str | None


@dataclass(order=True, slots=True)
class _QueueItem:
    priority: int
    sequence: int
    job: _Job[Any] | None = field(compare=False, default=None)


class HubRequestScheduler:
    """Own a bounded set of request workers for one config entry."""

    def __init__(
        self,
        *,
        worker_count: int = 2,
        max_queue_size: int = 128,
        close_timeout: float = 5.0,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self._configured_worker_count = worker_count
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue(
            maxsize=max_queue_size
        )
        self._close_timeout = close_timeout
        self._sequence = count()
        self._workers: set[asyncio.Task[None]] = set()
        self._futures: set[asyncio.Future[Any]] = set()
        self._singleflight: dict[str, asyncio.Future[Any]] = {}
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._running_count = 0
        self._running_high_watermark = 0
        self._queue_high_watermark = 0

    @property
    def stats(self) -> SchedulerStats:
        """Return a content-free diagnostic snapshot."""
        return SchedulerStats(
            queue_size=self._queue.qsize(),
            queue_high_watermark=self._queue_high_watermark,
            running_count=self._running_count,
            high_watermark=self._running_high_watermark,
            worker_count=sum(not worker.done() for worker in self._workers),
            singleflight_count=len(self._singleflight),
            closed=self._closed,
        )

    async def async_start(self) -> None:
        """Start owned workers once."""
        if self._closed:
            raise EzvizSchedulerClosedError("Request scheduler is closed")
        if self._workers:
            return
        async with self._start_lock:
            if self._closed:
                raise EzvizSchedulerClosedError("Request scheduler is closed")
            if self._workers:
                return
            self._workers = {
                asyncio.create_task(
                    self._async_worker(index),
                    name=f"ezviz-iot-cn-request-{index}",
                )
                for index in range(self._configured_worker_count)
            }

    async def async_submit[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        priority: RequestPriority = RequestPriority.STATE,
    ) -> T:
        """Submit one bounded job and await its result."""
        future = await self._async_enqueue(operation, priority, None)
        return await asyncio.shield(future)

    async def async_submit_singleflight[T](
        self,
        key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        priority: RequestPriority = RequestPriority.STATE,
    ) -> T:
        """Share one queued/running operation among callers with the same key."""
        if self._closed:
            raise EzvizSchedulerClosedError("Request scheduler is closed")
        existing = self._singleflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)
        future = await self._async_enqueue(operation, priority, key)
        return await asyncio.shield(future)

    async def _async_enqueue[T](
        self,
        operation: Callable[[], Awaitable[T]],
        priority: RequestPriority,
        singleflight_key: str | None,
    ) -> asyncio.Future[T]:
        if self._closed:
            raise EzvizSchedulerClosedError("Request scheduler is closed")
        await self.async_start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        job = _Job(
            operation=operation,
            future=future,
            singleflight_key=singleflight_key,
        )
        item = _QueueItem(int(priority), next(self._sequence), job)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as err:
            raise EzvizQueueFullError("EZVIZ request queue is full") from err
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)
        if singleflight_key is not None:
            self._singleflight[singleflight_key] = future
        self._queue_high_watermark = max(
            self._queue_high_watermark,
            self._queue.qsize(),
        )
        return future

    async def _async_worker(self, _index: int) -> None:
        while True:
            item = await self._queue.get()
            if item.job is None:
                self._queue.task_done()
                return
            job = item.job
            self._running_count += 1
            self._running_high_watermark = max(
                self._running_high_watermark,
                self._running_count,
            )
            try:
                result = await job.operation()
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                self._set_closed_exception(job.future)
                raise
            except Exception as err:
                if not job.future.done():
                    job.future.set_exception(err)
            finally:
                self._running_count -= 1
                if job.singleflight_key is not None:
                    current = self._singleflight.get(job.singleflight_key)
                    if current is job.future:
                        self._singleflight.pop(job.singleflight_key, None)
                self._queue.task_done()

    async def async_close(self) -> None:
        """Reject new work and settle all owned jobs within a finite time."""
        async with self._close_lock:
            if self._closed and not self._workers:
                return
            self._closed = True
            self._reject_queued_jobs()
            workers = tuple(self._workers)
            for _ in workers:
                self._queue.put_nowait(
                    _QueueItem(
                        int(RequestPriority.SCHEDULED_CAPTURE) + 1,
                        next(self._sequence),
                    )
                )
            if workers:
                done, pending = await asyncio.wait(
                    workers,
                    timeout=self._close_timeout,
                )
                del done
                for worker in pending:
                    worker.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            for future in tuple(self._futures):
                self._set_closed_exception(future)
            self._singleflight.clear()
            self._workers.clear()
            self._reject_queued_jobs()

    def _reject_queued_jobs(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item.job is not None:
                self._set_closed_exception(item.job.future)
                if item.job.singleflight_key is not None:
                    current = self._singleflight.get(item.job.singleflight_key)
                    if current is item.job.future:
                        self._singleflight.pop(item.job.singleflight_key, None)
            self._queue.task_done()

    @staticmethod
    def _set_closed_exception(future: asyncio.Future[Any]) -> None:
        if not future.done():
            future.set_exception(
                EzvizSchedulerClosedError("Request scheduler was closed")
            )
