"""Controls backed by verified C3C night-vision readback."""

from __future__ import annotations

from dataclasses import replace
from typing import override

from homeassistant.exceptions import HomeAssistantError

from .api.models import EzvizDevice, NightVisionMode, NightVisionSettings
from .entity import EzvizDeviceEntity, async_execute_control
from .runtime import EzvizConfigEntry

NIGHT_VISION_OPTIONS = {
    "full_color": NightVisionMode.FULL_COLOR,
    "monochrome": NightVisionMode.MONOCHROME,
    "smart": NightVisionMode.SMART,
}
NIGHT_VISION_OPTION_BY_MODE = {
    mode: option for option, mode in NIGHT_VISION_OPTIONS.items()
}


class EzvizNightVisionEntity(EzvizDeviceEntity):
    """Share settings and conservative writes across all C3C controls."""

    def __init__(self, entry: EzvizConfigEntry, device: EzvizDevice, key: str) -> None:
        super().__init__(entry, device, key)
        self._entry = entry

    @property
    def settings(self) -> NightVisionSettings | None:
        """Return the last settings read from this device."""
        return next(
            (
                device.night_vision
                for device in self._metadata_coordinator.data.devices
                if device.serial == self._device_serial and not device.is_shared
            ),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Only permit writes while the current device and settings are known."""
        return (
            super().available
            and self._metadata_coordinator.last_update_success
            and (device := self.current_device) is not None
            and device.online
            and self.settings is not None
        )

    async def async_update_settings(
        self,
        *,
        mode: NightVisionMode | None = None,
        duration: int | None = None,
        brightness: int | None = None,
    ) -> None:
        """Apply the requested adjustment and publish verified readback."""
        if not self.available:
            raise HomeAssistantError("Night-vision settings are not available")

        async def apply() -> None:
            observed = await self._entry.runtime_data.client.async_update_night_vision(
                self._device_serial,
                mode=mode,
                duration=duration,
                brightness=brightness,
            )
            snapshot = self._metadata_coordinator.data
            self._metadata_coordinator.async_set_updated_data(
                replace(
                    snapshot,
                    devices=tuple(
                        replace(device, night_vision=observed)
                        if device.serial == self._device_serial
                        else device
                        for device in snapshot.devices
                    ),
                )
            )

        await async_execute_control(self._entry, apply, action="update night vision")
