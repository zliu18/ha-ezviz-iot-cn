"""Bounded in-memory image entities for EZVIZ IoT CN."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import override

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.errors import EzvizError
from .api.models import EzvizChannel, EzvizDevice
from .api.scheduler import RequestPriority
from .const import (
    CONF_CAPTURE_CHANNELS,
    CONF_CAPTURE_INTERVAL,
    CONF_ENABLE_ALARM_IMAGES,
    CONF_ENABLE_SCHEDULED_CAPTURES,
    DEFAULT_CAPTURE_INTERVAL,
    MAX_CAPTURE_INTERVAL,
    MIN_CAPTURE_INTERVAL,
)
from .coordinator import MetadataSnapshot, capture_targets
from .entity import (
    EzvizChannelEntity,
    async_setup_dynamic_entities,
    devices_by_serial,
)
from .event import ReconciledAlarmState, ReconciledCallingState
from .runtime import (
    EzvizConfigEntry,
    ImageBuffer,
    ImageCacheKey,
    ImageKind,
)
from .semantics import ObservedSemantic

_LOGGER = logging.getLogger(__name__)

CAPTURE_DESCRIPTION = ImageEntityDescription(
    key="latest_capture",
    translation_key="latest_capture",
)
ALARM_DESCRIPTION = ImageEntityDescription(
    key="latest_alarm",
    translation_key="latest_alarm",
)
CALLING_DESCRIPTION = ImageEntityDescription(
    key="latest_calling",
    translation_key="latest_calling",
)

SleepCallable = Callable[[float], Awaitable[None]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up option- and capability-driven image entities."""
    coordinator = entry.runtime_data.metadata_coordinator

    @callback
    def prune_cache() -> None:
        entry.runtime_data.image_cache.prune(
            _cache_keys_for_snapshot(entry, coordinator.data)
        )

    prune_cache()
    entry.async_on_unload(coordinator.async_add_listener(prune_cache))
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(hass, entry, snapshot),
    )
    if (
        entry.options.get(CONF_ENABLE_SCHEDULED_CAPTURES, False)
        and entry.runtime_data.capture_task is None
    ):
        entry.runtime_data.capture_task = asyncio.create_task(
            async_capture_loop(entry),
            name=f"ezviz-iot-cn-capture-{entry.entry_id}",
        )


def _entities_for_snapshot(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[ImageEntity]:
    devices = devices_by_serial(snapshot)
    scheduled = (
        set(capture_targets(snapshot, entry.options.get(CONF_CAPTURE_CHANNELS, ())))
        if entry.options.get(CONF_ENABLE_SCHEDULED_CAPTURES, False)
        else set()
    )
    event_images_enabled = bool(entry.options.get(CONF_ENABLE_ALARM_IMAGES, True))
    observed = entry.runtime_data.observed_registry
    entities: list[ImageEntity] = []
    for channel in snapshot.channels:
        device = devices.get(channel.device_serial)
        if device is None:
            continue
        if channel in scheduled:
            entities.append(
                EzvizCachedImage(
                    hass,
                    entry,
                    device,
                    channel,
                    CAPTURE_DESCRIPTION,
                    ImageKind.CAPTURE,
                )
            )
        if event_images_enabled and observed.has(
            device.serial,
            channel.channel_no,
            ObservedSemantic.ALARM_IMAGE,
        ):
            entities.append(
                EzvizCachedImage(
                    hass,
                    entry,
                    device,
                    channel,
                    ALARM_DESCRIPTION,
                    ImageKind.ALARM,
                )
            )
        if event_images_enabled and observed.has(
            device.serial,
            channel.channel_no,
            ObservedSemantic.CALLING_IMAGE,
        ):
            entities.append(
                EzvizCachedImage(
                    hass,
                    entry,
                    device,
                    channel,
                    CALLING_DESCRIPTION,
                    ImageKind.CALLING,
                )
            )
    return entities


def _cache_keys_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> set[ImageCacheKey]:
    devices = devices_by_serial(snapshot)
    scheduled = (
        set(capture_targets(snapshot, entry.options.get(CONF_CAPTURE_CHANNELS, ())))
        if entry.options.get(CONF_ENABLE_SCHEDULED_CAPTURES, False)
        else set()
    )
    event_images_enabled = bool(entry.options.get(CONF_ENABLE_ALARM_IMAGES, True))
    observed = entry.runtime_data.observed_registry
    keys: set[ImageCacheKey] = set()
    for channel in snapshot.channels:
        if channel.device_serial not in devices:
            continue
        if event_images_enabled and observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.ALARM_IMAGE,
        ):
            keys.add(
                ImageCacheKey(
                    channel.device_serial,
                    channel.channel_no,
                    ImageKind.ALARM,
                )
            )
        if event_images_enabled and observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.CALLING_IMAGE,
        ):
            keys.add(
                ImageCacheKey(
                    channel.device_serial,
                    channel.channel_no,
                    ImageKind.CALLING,
                )
            )
        if channel in scheduled:
            keys.add(
                ImageCacheKey(
                    channel.device_serial,
                    channel.channel_no,
                    ImageKind.CAPTURE,
                )
            )
    return keys


class EzvizCachedImage(EzvizChannelEntity, ImageEntity):
    """Latest scheduled or event-derived image for one channel."""

    entity_description: ImageEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        description: ImageEntityDescription,
        kind: ImageKind,
    ) -> None:
        self.entity_description = description
        EzvizChannelEntity.__init__(
            self,
            entry,
            device,
            channel,
            description.key,
            capability_check=(
                (lambda capabilities: capabilities.supports_capture)
                if kind is ImageKind.CAPTURE
                else None
            ),
        )
        ImageEntity.__init__(self, hass)
        self._entry = entry
        self._kind = kind
        self._reconciler = entry.runtime_data.reconciler
        self._cache_key = ImageCacheKey(device.serial, channel.channel_no, kind)

    @property
    def _buffer(self) -> ImageBuffer | None:
        return self._entry.runtime_data.image_cache.get(self._cache_key)

    @property
    @override
    def content_type(self) -> str:
        """Return cached content type without exposing the source URL."""
        if (buffer := self._buffer) is not None:
            return buffer.content_type
        return "image/jpeg"

    @property
    @override
    def available(self) -> bool:
        """Expose only a real cache value or downloadable event image."""
        if not super().available:
            return False
        if self._buffer is not None:
            return True
        state = self._event_image_state
        return state is not None and state[0] is not None

    @property
    @override
    def image_last_updated(self) -> datetime | None:
        """Return the current cache timestamp."""
        if (buffer := self._buffer) is not None:
            return buffer.updated_at
        return None

    @override
    async def async_added_to_hass(self) -> None:
        """Listen only to this channel and image kind while loaded."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._entry.runtime_data.image_cache.subscribe(
                self._cache_key,
                self._handle_cache_update,
            )
        )
        if self._kind is not ImageKind.CAPTURE:
            self.async_on_remove(self._reconciler.subscribe(self._handle_event_update))

    @callback
    def _handle_cache_update(self, _buffer: ImageBuffer) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_event_update(
        self,
        state: ReconciledAlarmState | ReconciledCallingState,
    ) -> None:
        expected_type = (
            ReconciledAlarmState
            if self._kind is ImageKind.ALARM
            else ReconciledCallingState
        )
        if (
            isinstance(state, expected_type)
            and state.device_serial == self._device_serial
            and state.channel_no == self._channel_no
        ):
            self.async_write_ha_state()

    @override
    async def async_image(self) -> bytes | None:
        """Return a cache value, downloading newer event images on demand."""
        if self._kind is not ImageKind.CAPTURE:
            await self._async_update_event_image()
        if (buffer := self._buffer) is not None:
            return buffer.content
        return None

    @property
    def _event_image_state(self) -> tuple[str | None, datetime] | None:
        if self._kind is ImageKind.ALARM:
            alarm_state = self._reconciler.alarm_state_for(
                self._device_serial,
                self._channel_no,
            )
            if alarm_state is None:
                return None
            return (
                alarm_state.latest_alarm_image_url,
                alarm_state.last_alarm_time,
            )
        if self._kind is ImageKind.CALLING:
            calling_state = self._reconciler.calling_state_for(
                self._device_serial,
                self._channel_no,
            )
            if calling_state is None:
                return None
            return (
                calling_state.latest_calling_image_url,
                calling_state.last_calling_time,
            )
        return None

    async def _async_update_event_image(self) -> None:
        state = self._event_image_state
        if state is None:
            return
        image_url, event_time = state
        if image_url is None:
            return
        if (buffer := self._buffer) is not None and buffer.updated_at >= event_time:
            return

        async def download() -> tuple[bytes, str]:
            return await self._entry.runtime_data.client.async_download_image(image_url)

        try:
            (
                content,
                content_type,
            ) = await self._entry.runtime_data.scheduler.async_submit(
                download,
                priority=RequestPriority.ALARM,
            )
        except EzvizError:
            _LOGGER.debug("Unable to refresh an EZVIZ event image")
            return
        self._entry.runtime_data.image_cache.set(
            self._cache_key,
            content,
            content_type,
            event_time,
        )


def _capture_interval(entry: EzvizConfigEntry) -> int:
    value = entry.options.get(CONF_CAPTURE_INTERVAL, DEFAULT_CAPTURE_INTERVAL)
    try:
        interval = int(value)
    except TypeError, ValueError:
        interval = DEFAULT_CAPTURE_INTERVAL
    return max(MIN_CAPTURE_INTERVAL, min(MAX_CAPTURE_INTERVAL, interval))


async def async_capture_cycle(
    entry: EzvizConfigEntry,
    *,
    sleep: SleepCallable = asyncio.sleep,
) -> None:
    """Capture current supported channels sequentially across one interval."""
    targets = capture_targets(
        entry.runtime_data.metadata_coordinator.data,
        entry.options.get(CONF_CAPTURE_CHANNELS, ()),
    )
    interval = _capture_interval(entry)
    if not targets:
        await sleep(float(interval))
        return
    spacing = interval / len(targets)
    for channel in targets:
        await _async_capture_channel(entry, channel)
        await sleep(spacing)


async def _async_capture_channel(
    entry: EzvizConfigEntry,
    channel: EzvizChannel,
) -> None:
    async def capture_and_download() -> tuple[bytes, str]:
        capture = await entry.runtime_data.client.async_capture(
            channel.device_serial,
            channel.channel_no,
        )
        return await entry.runtime_data.client.async_download_image(capture.image_url)

    try:
        content, content_type = await entry.runtime_data.scheduler.async_submit(
            capture_and_download,
            priority=RequestPriority.SCHEDULED_CAPTURE,
        )
    except EzvizError:
        _LOGGER.debug("Unable to refresh a scheduled EZVIZ capture")
        return
    entry.runtime_data.image_cache.set(
        ImageCacheKey(
            channel.device_serial,
            channel.channel_no,
            ImageKind.CAPTURE,
        ),
        content,
        content_type,
        datetime.now(tz=UTC),
    )


async def async_capture_loop(entry: EzvizConfigEntry) -> None:
    """Run staggered capture cycles until config-entry unload cancellation."""
    while True:
        await async_capture_cycle(entry)
