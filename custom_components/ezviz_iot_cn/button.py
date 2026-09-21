"""PTZ button controls for EZVIZ IoT CN."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import EzvizChannel, EzvizDevice, PtzDirection
from .coordinator import MetadataSnapshot
from .entity import (
    EzvizChannelEntity,
    EzvizDeviceEntity,
    async_execute_control,
    async_setup_dynamic_entities,
    capabilities_by_serial,
    devices_by_serial,
    hub_device_info,
)
from .runtime import EzvizConfigEntry

PTZ_MOVE_SECONDS = 0.5
PTZ_SPEED = 1
REFRESH_DEVICE_DESCRIPTION = ButtonEntityDescription(
    key="refresh_device_state",
    translation_key="refresh_device_state",
    entity_category=EntityCategory.DIAGNOSTIC,
)
REFRESH_USAGE_DESCRIPTION = ButtonEntityDescription(
    key="refresh_api_usage",
    translation_key="refresh_api_usage",
    entity_category=EntityCategory.DIAGNOSTIC,
)


@dataclass(frozen=True, kw_only=True)
class EzvizPtzButtonDescription(ButtonEntityDescription):
    """Describe one bounded PTZ direction button."""

    direction: PtzDirection


PTZ_UP = EzvizPtzButtonDescription(
    key="ptz_up",
    translation_key="ptz_up",
    direction=PtzDirection.UP,
)
PTZ_DOWN = EzvizPtzButtonDescription(
    key="ptz_down",
    translation_key="ptz_down",
    direction=PtzDirection.DOWN,
)
PTZ_LEFT = EzvizPtzButtonDescription(
    key="ptz_left",
    translation_key="ptz_left",
    direction=PtzDirection.LEFT,
)
PTZ_RIGHT = EzvizPtzButtonDescription(
    key="ptz_right",
    translation_key="ptz_right",
    direction=PtzDirection.RIGHT,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up capability-driven EZVIZ PTZ buttons."""
    del hass
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(entry, snapshot),
    )
    async_add_entities([EzvizRefreshUsageButton(entry)])


def _entities_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[ButtonEntity]:
    devices = devices_by_serial(snapshot)
    capabilities = capabilities_by_serial(snapshot)
    entities: list[ButtonEntity] = []
    for channel in snapshot.channels:
        device = devices.get(channel.device_serial)
        device_capabilities = capabilities.get(channel.device_serial)
        if (
            device is None
            or device_capabilities is None
            or not device_capabilities.supports_ptz
        ):
            continue
        descriptions: list[EzvizPtzButtonDescription] = []
        if device_capabilities.ptz_top_bottom:
            descriptions.extend((PTZ_UP, PTZ_DOWN))
        if device_capabilities.ptz_left_right:
            descriptions.extend((PTZ_LEFT, PTZ_RIGHT))
        entities.extend(
            EzvizPtzButton(entry, device, channel, description)
            for description in descriptions
        )
    entities.extend(
        EzvizRefreshDeviceButton(entry, device) for device in snapshot.devices
    )
    return entities


class EzvizRefreshDeviceButton(EzvizDeviceEntity, ButtonEntity):
    """Run one account-batched refresh from a physical device."""

    entity_description = REFRESH_DEVICE_DESCRIPTION

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice) -> None:
        super().__init__(entry, device, self.entity_description.key)
        self._entry = entry

    @property
    @override
    def available(self) -> bool:
        """Allow recovery even while the account state coordinator is failing."""
        snapshot = self._metadata_coordinator.data
        return snapshot is not None and any(
            device.serial == self._device_serial for device in snapshot.devices
        )

    @override
    async def async_press(self) -> None:
        await self._entry.runtime_data.async_refresh_device_state()


class EzvizRefreshUsageButton(ButtonEntity):
    """Publish local counters immediately without requesting vendor quota."""

    entity_description = REFRESH_USAGE_DESCRIPTION
    _attr_has_entity_name = True

    def __init__(self, entry: EzvizConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}:refresh_api_usage"
        self._attr_device_info = hub_device_info(entry)

    @override
    async def async_press(self) -> None:
        self._entry.runtime_data.client.usage_tracker.refresh()


class EzvizPtzButton(EzvizChannelEntity, ButtonEntity):
    """Bounded PTZ movement control for one camera channel."""

    entity_description: EzvizPtzButtonDescription

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        description: EzvizPtzButtonDescription,
    ) -> None:
        self.entity_description = description
        super().__init__(
            entry,
            device,
            channel,
            description.key,
            capability_check=lambda capabilities: (
                capabilities.supports_ptz
                and (
                    capabilities.ptz_top_bottom
                    if description.direction in (PtzDirection.UP, PtzDirection.DOWN)
                    else capabilities.ptz_left_right
                )
            ),
        )
        self._entry = entry

    @override
    async def async_press(self) -> None:
        """Run one bounded movement and always issue its matching stop."""
        await self._async_start_movement(self.entity_description.direction)

    async def _async_start_movement(self, direction: PtzDirection) -> None:
        runtime = self._entry.runtime_data
        key = (self._device_serial, self._channel_no)
        lock = runtime.ptz_locks.setdefault(key, asyncio.Lock())
        async with lock:
            previous = runtime.ptz_tasks.get(key)
            if previous is not None and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            task = asyncio.create_task(
                self._async_move(direction),
                name=f"ezviz-iot-cn-ptz-{self._device_id}-{self._channel_no}",
            )
            runtime.ptz_tasks[key] = task
        try:
            await task
        finally:
            if runtime.ptz_tasks.get(key) is task:
                runtime.ptz_tasks.pop(key, None)

    async def _async_move(self, direction: PtzDirection) -> None:
        start_task = asyncio.create_task(
            async_execute_control(
                self._entry,
                lambda: self._entry.runtime_data.client.async_ptz_start(
                    self._device_serial,
                    channel_no=self._channel_no,
                    direction=direction,
                    speed=PTZ_SPEED,
                ),
                action="start PTZ movement",
            ),
            name=f"ezviz-iot-cn-ptz-start-{self._device_id}-{self._channel_no}",
        )
        try:
            await asyncio.shield(start_task)
            await asyncio.sleep(PTZ_MOVE_SECONDS)
        finally:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(start_task)
            await self._async_stop_direction(direction)

    async def _async_stop_direction(self, direction: PtzDirection) -> None:
        await async_execute_control(
            self._entry,
            lambda: self._entry.runtime_data.client.async_ptz_stop(
                self._device_serial,
                channel_no=self._channel_no,
                direction=direction,
            ),
            action="stop PTZ movement",
        )
