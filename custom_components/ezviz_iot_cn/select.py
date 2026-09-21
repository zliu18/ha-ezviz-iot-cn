"""Select controls for EZVIZ IoT CN."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import AlarmSoundMode, EzvizDevice
from .coordinator import MetadataSnapshot
from .entity import (
    EzvizDeviceEntity,
    async_execute_control,
    async_setup_dynamic_entities,
    capabilities_by_serial,
)
from .runtime import EzvizConfigEntry

ALARM_SOUND_DESCRIPTION = SelectEntityDescription(
    key="alarm_sound_mode",
    translation_key="alarm_sound_mode",
)

ALARM_SOUND_OPTIONS: dict[str, AlarmSoundMode] = {
    "short_alert": AlarmSoundMode.SHORT_ALERT,
    "long_alert": AlarmSoundMode.LONG_ALERT,
    "silent": AlarmSoundMode.SILENT,
}
ALARM_SOUND_OPTION_BY_MODE = {
    mode: option for option, mode in ALARM_SOUND_OPTIONS.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up capability-driven EZVIZ alarm-sound selects."""
    del hass
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda snapshot: _entities_for_snapshot(entry, snapshot),
    )


def _entities_for_snapshot(
    entry: EzvizConfigEntry,
    snapshot: MetadataSnapshot,
) -> Iterable[SelectEntity]:
    capabilities = capabilities_by_serial(snapshot)
    return (
        EzvizAlarmSoundSelect(entry, device)
        for device in snapshot.devices
        if not device.is_shared
        and device.alarm_sound_mode is not None
        and (capability := capabilities.get(device.serial)) is not None
        and capability.supports_alarm_sound
    )


class EzvizAlarmSoundSelect(EzvizDeviceEntity, SelectEntity):
    """Documented alarm-sound mode control for one supported device."""

    entity_description = ALARM_SOUND_DESCRIPTION

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice) -> None:
        super().__init__(entry, device, self.entity_description.key)
        self._entry = entry
        self._attr_options = list(ALARM_SOUND_OPTIONS)

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
    def current_option(self) -> str | None:
        """Return the translated option key from verified device status."""
        device = self._metadata_device
        if device is None or device.alarm_sound_mode is None:
            return None
        return ALARM_SOUND_OPTION_BY_MODE.get(device.alarm_sound_mode)

    @property
    @override
    def available(self) -> bool:
        """Expose the control only while the API reports a supported mode."""
        device = self._metadata_device
        return (
            super().available
            and self._metadata_coordinator.last_update_success
            and device is not None
            and not device.is_shared
            and device.alarm_sound_mode is not None
            and (capability := self.current_capabilities) is not None
            and capability.supports_alarm_sound
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Set a documented sound mode and refresh verified device status."""
        if not self.available:
            raise HomeAssistantError("Alarm-sound state is not verified")
        try:
            mode = ALARM_SOUND_OPTIONS[option]
        except KeyError as err:
            raise ValueError(f"Unsupported alarm sound option: {option}") from err
        await async_execute_control(
            self._entry,
            lambda: self._entry.runtime_data.client.async_set_alarm_sound(
                self._device_serial,
                mode,
            ),
            action="set alarm sound mode",
        )
        await self._metadata_coordinator.async_request_refresh()
