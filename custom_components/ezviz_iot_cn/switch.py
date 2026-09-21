"""Switch controls for EZVIZ IoT CN."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import EzvizDevice
from .coordinator import MetadataSnapshot
from .entity import (
    EzvizDeviceEntity,
    async_execute_control,
    async_setup_dynamic_entities,
    capabilities_by_serial,
)
from .runtime import EzvizConfigEntry

DEFENCE_DESCRIPTION = SwitchEntityDescription(
    key="defence",
    translation_key="defence",
)
ALARM_FLASH_DESCRIPTION = SwitchEntityDescription(
    key="alarm_flash",
    translation_key="alarm_flash",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up independently verified EZVIZ alarm controls."""
    del hass
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(entry, snapshot),
    )


def _entities_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[SwitchEntity]:
    capabilities = capabilities_by_serial(snapshot)
    defence = (
        EzvizDefenceSwitch(entry, device)
        for device in snapshot.devices
        if (device_capabilities := capabilities.get(device.serial)) is not None
        and device_capabilities.supports_defence
    )
    flash = (
        EzvizAlarmFlashSwitch(entry, device)
        for device in snapshot.devices
        if not device.is_shared
        and device.alarm_light_protocol is not None
        and (
            (capability := capabilities.get(device.serial)) is None
            or capability.supports_alarm_light
        )
    )
    return (*defence, *flash)


class EzvizDefenceSwitch(EzvizDeviceEntity, SwitchEntity):
    """Cloud alarm arming control for one supported device."""

    entity_description = DEFENCE_DESCRIPTION

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice) -> None:
        super().__init__(
            entry,
            device,
            self.entity_description.key,
            capability_check=lambda capabilities: capabilities.supports_defence,
        )
        self._entry = entry

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the last account state until the post-write refresh completes."""
        device = self.current_device
        if device is None or device.defence is None:
            return None
        return bool(device.defence)

    @property
    @override
    def available(self) -> bool:
        """Expose the control only with a real current defence state."""
        device = self.current_device
        return super().available and device is not None and device.defence is not None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable cloud alarm arming."""
        del kwargs
        await self._async_set_defence(enabled=True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable cloud alarm arming."""
        del kwargs
        await self._async_set_defence(enabled=False)

    async def _async_set_defence(self, *, enabled: bool) -> None:
        await async_execute_control(
            self._entry,
            lambda: self._entry.runtime_data.client.async_set_defence(
                self._device_serial,
                enabled=enabled,
            ),
            action="set defence state",
        )
        await self.coordinator.async_request_refresh()


class EzvizAlarmFlashSwitch(EzvizDeviceEntity, SwitchEntity):
    """Alarm-light setting, separate from arming and alarm sound."""

    entity_description = ALARM_FLASH_DESCRIPTION

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice) -> None:
        super().__init__(
            entry,
            device,
            self.entity_description.key,
            capability_check=lambda capability: capability.supports_alarm_light,
        )
        self._entry = entry

    @property
    def _metadata_device(self) -> EzvizDevice | None:
        return next(
            (
                device
                for device in self._metadata_coordinator.data.devices
                if device.serial == self._device_serial
            ),
            None,
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return only the last public status read, never an optimistic write."""
        device = self._metadata_device
        return None if device is None else device.alarm_light_enabled

    @property
    @override
    def available(self) -> bool:
        """Keep verified identity, but require a fresh readable state to write."""
        device = self._metadata_device
        current = self.current_device
        return (
            super().available
            and device is not None
            and not device.is_shared
            and device.online
            and current is not None
            and current.online
            and device.alarm_light_protocol is not None
            and device.alarm_light_enabled is not None
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_set(enabled=True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        await self._async_set(enabled=False)

    async def _async_set(self, *, enabled: bool) -> None:
        device = self._metadata_device
        protocol = None if device is None else device.alarm_light_protocol
        if not self.available or protocol is None:
            raise HomeAssistantError("Alarm-light state is not verified")
        await async_execute_control(
            self._entry,
            lambda: self._entry.runtime_data.client.async_set_alarm_light(
                self._device_serial,
                protocol=protocol,
                enabled=enabled,
            ),
            action="set alarm flash",
        )
        await self._metadata_coordinator.async_request_refresh()
