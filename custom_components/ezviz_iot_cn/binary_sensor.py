"""Binary sensors for EZVIZ IoT CN."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import EzvizChannel, EzvizDevice
from .coordinator import MetadataSnapshot
from .entity import (
    EzvizAlarmEntity,
    EzvizCallingEntity,
    EzvizDeviceEntity,
    async_setup_dynamic_entities,
    devices_by_serial,
)
from .runtime import EzvizConfigEntry
from .semantics import ObservedSemantic

ONLINE_DESCRIPTION = BinarySensorEntityDescription(
    key="online",
    translation_key="online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)
MOTION_DESCRIPTION = BinarySensorEntityDescription(
    key="motion",
    translation_key="motion",
    device_class=BinarySensorDeviceClass.MOTION,
)
PERSON_DESCRIPTION = BinarySensorEntityDescription(
    key="person",
    translation_key="person",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
)
DOORBELL_RING_DESCRIPTION = BinarySensorEntityDescription(
    key="doorbell_ring",
    translation_key="doorbell_ring",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up capability-driven EZVIZ binary sensors."""
    del hass
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(entry, snapshot),
    )


def _entities_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[BinarySensorEntity]:
    devices = devices_by_serial(snapshot)
    entities: list[BinarySensorEntity] = [
        EzvizOnlineBinarySensor(entry, device) for device in snapshot.devices
    ]
    observed = entry.runtime_data.observed_registry
    for channel in snapshot.channels:
        device = devices.get(channel.device_serial)
        if device is None:
            continue
        if observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.MOTION,
        ):
            entities.append(EzvizAlarmBinarySensor(entry, device, channel, "motion"))
        if observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.PERSON,
        ):
            entities.append(EzvizAlarmBinarySensor(entry, device, channel, "person"))
        if observed.has(
            channel.device_serial,
            channel.channel_no,
            ObservedSemantic.DOORBELL_RING,
        ):
            entities.append(EzvizDoorbellRingBinarySensor(entry, device, channel))
    return entities


class EzvizOnlineBinarySensor(EzvizDeviceEntity, BinarySensorEntity):
    """Device connectivity reported by the Open Platform."""

    entity_description = ONLINE_DESCRIPTION

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice) -> None:
        super().__init__(entry, device, self.entity_description.key)

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the current device snapshot reports online."""
        device = self.current_device
        return device.online if device is not None else False


class EzvizAlarmBinarySensor(EzvizAlarmEntity, BinarySensorEntity):
    """Transient motion or person state for one device channel."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        kind: str,
    ) -> None:
        description = MOTION_DESCRIPTION if kind == "motion" else PERSON_DESCRIPTION
        self.entity_description = description
        self._kind = kind
        super().__init__(entry, device, channel, description.key)

    @property
    @override
    def is_on(self) -> bool:
        """Return current reconciled transient alarm state."""
        state = self.alarm_state
        return bool(state and getattr(state, self._kind))


class EzvizDoorbellRingBinarySensor(EzvizCallingEntity, BinarySensorEntity):
    """Transient ring state after a demonstrated doorbell calling event."""

    entity_description = DOORBELL_RING_DESCRIPTION

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
    ) -> None:
        super().__init__(entry, device, channel, self.entity_description.key)

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the current calling lifecycle is ringing."""
        state = self.calling_state
        return bool(state and state.ringing)
