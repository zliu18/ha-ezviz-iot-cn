"""Typed config-entry runtime state for EZVIZ IoT CN."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from .api.client import EzvizApiClient
from .api.image_downloader import MAX_IMAGE_BYTES
from .api.scheduler import HubRequestScheduler
from .coordinator import (
    EzvizAlarmCoordinator,
    EzvizMetadataCoordinator,
    EzvizStateCoordinator,
)
from .event import EventReconciler
from .persistence import EventStateStore
from .semantics import ObservedEntityRegistry

if TYPE_CHECKING:
    from .webhook import EzvizPushEnvelope

type PtzKey = tuple[str, int]
MANUAL_REFRESH_COOLDOWN_SECONDS = 60


class ImageKind(StrEnum):
    """Independent in-memory image slots for each channel."""

    CAPTURE = "capture"
    ALARM = "alarm"
    CALLING = "calling"


@dataclass(frozen=True, slots=True)
class ImageCacheKey:
    """Stable in-memory image key without URLs or credentials."""

    device_serial: str
    channel_no: int
    kind: ImageKind


@dataclass(frozen=True, slots=True)
class ImageBuffer:
    """One bounded image value held only in memory."""

    content: bytes
    content_type: str
    updated_at: datetime


ImageListener = Callable[[ImageBuffer], None]


@dataclass(slots=True)
class WebhookStats:
    """Bounded aggregate Cloud Push processing counters."""

    valid_envelopes: int = 0
    unsupported_types: int = 0
    unknown_targets: int = 0
    invalid_messages: int = 0
    worker_errors: int = 0
    queue_drops: int = 0
    processed_alarms: int = 0
    processed_calls: int = 0


class EzvizImageCache:
    """Keep bounded, independent image slots per configured channel."""

    def __init__(self) -> None:
        self._buffers: dict[ImageCacheKey, ImageBuffer] = {}
        self._listeners: dict[ImageCacheKey, set[ImageListener]] = {}

    @property
    def count(self) -> int:
        """Return the current bounded slot count for diagnostics and tests."""
        return len(self._buffers)

    def get(self, key: ImageCacheKey) -> ImageBuffer | None:
        """Return one immutable image buffer."""
        return self._buffers.get(key)

    def set(
        self,
        key: ImageCacheKey,
        content: bytes,
        content_type: str,
        updated_at: datetime,
    ) -> None:
        """Replace one slot after enforcing content and memory bounds."""
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 8 MiB cache limit")
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if not normalized_type.startswith("image/"):
            raise ValueError("Image cache content type must be an image")
        buffer = ImageBuffer(content, normalized_type, updated_at)
        self._buffers[key] = buffer
        for listener in tuple(self._listeners.get(key, ())):
            listener(buffer)

    def subscribe(
        self,
        key: ImageCacheKey,
        listener: ImageListener,
    ) -> Callable[[], None]:
        """Subscribe to one slot and return an idempotent unsubscribe callback."""
        listeners = self._listeners.setdefault(key, set())
        listeners.add(listener)

        def unsubscribe() -> None:
            listeners.discard(listener)
            if not listeners:
                self._listeners.pop(key, None)

        return unsubscribe

    def prune(self, valid_keys: AbstractSet[ImageCacheKey]) -> None:
        """Release buffers that no longer belong to configured channels."""
        for key in self._buffers.keys() - valid_keys:
            self._buffers.pop(key, None)

    def clear(self) -> None:
        """Release all image bytes and listeners during unload."""
        self._buffers.clear()
        self._listeners.clear()


@dataclass(slots=True)
class EzvizRuntimeData:
    """All activity owned by one configured EZVIZ Open Platform hub."""

    client: EzvizApiClient
    scheduler: HubRequestScheduler
    reconciler: EventReconciler
    observed_registry: ObservedEntityRegistry
    event_store: EventStateStore
    metadata_coordinator: EzvizMetadataCoordinator
    state_coordinator: EzvizStateCoordinator
    alarm_coordinator: EzvizAlarmCoordinator
    hub_device_id: str | None = None
    image_cache: EzvizImageCache = field(default_factory=EzvizImageCache)
    capture_task: asyncio.Task[None] | None = None
    webhook_id: str | None = None
    webhook_queue: asyncio.Queue[EzvizPushEnvelope] | None = None
    webhook_task: asyncio.Task[None] | None = None
    webhook_stats: WebhookStats = field(default_factory=WebhookStats)
    ptz_tasks: dict[PtzKey, asyncio.Task[None]] = field(default_factory=dict)
    ptz_locks: dict[PtzKey, asyncio.Lock] = field(default_factory=dict)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_manual_refresh: float | None = None
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    async def async_refresh_device_state(self) -> None:
        """Refresh account batches and low-frequency controls once per hub."""
        async with self.refresh_lock:
            now = asyncio.get_running_loop().time()
            if (
                self._last_manual_refresh is not None
                and now - self._last_manual_refresh < MANUAL_REFRESH_COOLDOWN_SECONDS
            ):
                raise HomeAssistantError(
                    "Device refresh is limited to once per minute per hub"
                )
            self._last_manual_refresh = now
            failed = False
            for coordinator in (
                self.state_coordinator,
                self.alarm_coordinator,
                self.metadata_coordinator,
            ):
                try:
                    await coordinator.async_request_refresh()
                except Exception:
                    failed = True
                else:
                    failed |= not coordinator.last_update_success
            if failed:
                raise HomeAssistantError("Unable to refresh all EZVIZ device status")

    async def async_close(self) -> None:
        """Stop coordinators, event timers, and network workers once."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.capture_task is not None:
                    self.capture_task.cancel()
                    await asyncio.gather(self.capture_task, return_exceptions=True)
                    self.capture_task = None
                self.image_cache.clear()
                if self.webhook_task is not None:
                    self.webhook_task.cancel()
                    await asyncio.gather(self.webhook_task, return_exceptions=True)
                    self.webhook_task = None
                self.webhook_queue = None
                tasks = tuple(self.ptz_tasks.values())
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.ptz_tasks.clear()
                self.ptz_locks.clear()
                await asyncio.gather(
                    self.metadata_coordinator.async_shutdown(),
                    self.state_coordinator.async_shutdown(),
                    self.alarm_coordinator.async_shutdown(),
                )
                await self.event_store.async_flush()
                await self.reconciler.async_close()
                await self.scheduler.async_close()
            finally:
                await self.client.async_close()


type EzvizConfigEntry = ConfigEntry[EzvizRuntimeData]
