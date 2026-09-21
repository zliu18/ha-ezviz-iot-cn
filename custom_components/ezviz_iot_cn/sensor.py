"""Sensors for EZVIZ IoT CN."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import EzvizChannel, EzvizDevice
from .coordinator import MetadataSnapshot
from .entity import (
    EzvizAlarmEntity,
    EzvizCallingEntity,
    async_setup_dynamic_entities,
    devices_by_serial,
    hub_device_info,
)
from .runtime import EzvizConfigEntry
from .semantics import ObservedSemantic

ALARM_TYPE_DESCRIPTION = SensorEntityDescription(
    key="last_alarm_type",
    translation_key="last_alarm_type",
    entity_category=EntityCategory.DIAGNOSTIC,
)
ALARM_TIME_DESCRIPTION = SensorEntityDescription(
    key="last_alarm_time",
    translation_key="last_alarm_time",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)
CALLING_TIME_DESCRIPTION = SensorEntityDescription(
    key="last_calling_time",
    translation_key="last_calling_time",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)
CALLING_ACTION_DESCRIPTION = SensorEntityDescription(
    key="calling_action",
    translation_key="calling_action",
    device_class=SensorDeviceClass.ENUM,
    options=["ring", "answer", "hang_up"],
    entity_category=EntityCategory.DIAGNOSTIC,
)
API_ATTEMPTS_DESCRIPTION = SensorEntityDescription(
    key="local_api_requests",
    translation_key="local_api_requests",
    native_unit_of_measurement="requests",
    entity_category=EntityCategory.DIAGNOSTIC,
)
API_RATE_LIMITS_DESCRIPTION = SensorEntityDescription(
    key="local_api_rate_limits",
    translation_key="local_api_rate_limits",
    native_unit_of_measurement="events",
    entity_category=EntityCategory.DIAGNOSTIC,
)
USAGE_UPDATE_INTERVAL = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up read-only EZVIZ alarm sensors."""
    del hass
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(entry, snapshot),
    )
    async_add_entities(
        [
            EzvizLocalUsageSensor(entry, API_ATTEMPTS_DESCRIPTION),
            EzvizLocalUsageSensor(entry, API_RATE_LIMITS_DESCRIPTION),
        ]
    )


class EzvizLocalUsageSensor(SensorEntity):
    """Runtime-local observations; never an official quota balance."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: EzvizConfigEntry, description: SensorEntityDescription
    ) -> None:
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}:{description.key}"
        self._attr_device_info = hub_device_info(entry)
        self._last_update = 0.0
        self._pending: asyncio.TimerHandle | None = None

    @property
    @override
    def native_value(self) -> int:
        snapshot = self._entry.runtime_data.client.usage_snapshot
        if self.entity_description.key == "local_api_requests":
            return snapshot.request_attempts
        return snapshot.rate_limit_events

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._last_update = asyncio.get_running_loop().time()
        self.async_on_remove(
            self._entry.runtime_data.client.usage_tracker.subscribe(self._on_change)
        )
        self.async_on_remove(self._cancel_pending)

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _flush(self) -> None:
        self._pending = None
        self._last_update = asyncio.get_running_loop().time()
        self.async_write_ha_state()

    def _on_change(self, immediate: bool) -> None:
        now = asyncio.get_running_loop().time()
        if immediate or now - self._last_update >= USAGE_UPDATE_INTERVAL:
            self._cancel_pending()
            self._flush()
        elif self._pending is None:
            self._pending = asyncio.get_running_loop().call_later(
                USAGE_UPDATE_INTERVAL - (now - self._last_update), self._flush
            )


def _entities_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[SensorEntity]:
    devices = devices_by_serial(snapshot)
    observed = entry.runtime_data.observed_registry
    entities: list[SensorEntity] = []
    for channel in snapshot.channels:
        device = devices.get(channel.device_serial)
        if device is None:
            continue
        if observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.ALARM,
        ):
            entities.extend(
                (
                    EzvizLastAlarmSensor(
                        entry,
                        device,
                        channel,
                        ALARM_TYPE_DESCRIPTION,
                    ),
                    EzvizLastAlarmSensor(
                        entry,
                        device,
                        channel,
                        ALARM_TIME_DESCRIPTION,
                    ),
                )
            )
        if observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.CALLING,
        ):
            entities.extend(
                (
                    EzvizLastCallingSensor(
                        entry,
                        device,
                        channel,
                        CALLING_TIME_DESCRIPTION,
                    ),
                    EzvizLastCallingSensor(
                        entry,
                        device,
                        channel,
                        CALLING_ACTION_DESCRIPTION,
                    ),
                )
            )
    return entities


class EzvizLastAlarmSensor(EzvizAlarmEntity, SensorEntity):
    """Last alarm type or timestamp for one device channel."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        description: SensorEntityDescription,
    ) -> None:
        self.entity_description = description
        super().__init__(entry, device, channel, description.key)

    @property
    @override
    def native_value(self) -> str | datetime | None:
        """Return the selected field from reconciled alarm state."""
        state = self.alarm_state
        if state is None:
            return None
        if self.entity_description.key == "last_alarm_type":
            return state.last_alarm_type
        return state.last_alarm_time

    @property
    @override
    def available(self) -> bool:
        """Remain unavailable until an actual alarm has been reconciled."""
        return super().available and self.alarm_state is not None


class EzvizLastCallingSensor(EzvizCallingEntity, SensorEntity):
    """Last calling timestamp or lifecycle action for one channel."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        description: SensorEntityDescription,
    ) -> None:
        self.entity_description = description
        super().__init__(entry, device, channel, description.key)

    @property
    @override
    def native_value(self) -> str | datetime | None:
        """Return the selected field from reconciled calling state."""
        state = self.calling_state
        if state is None:
            return None
        if self.entity_description.key == "calling_action":
            return state.action.name.lower()
        return state.last_calling_time

    @property
    @override
    def available(self) -> bool:
        """Remain unavailable until an actual calling event is received."""
        return super().available and self.calling_state is not None
