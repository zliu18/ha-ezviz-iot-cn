"""C3C illumination duration and brightness controls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import EzvizDevice
from .coordinator import MetadataSnapshot
from .entity import async_setup_dynamic_entities
from .night_vision import EzvizNightVisionEntity
from .runtime import EzvizConfigEntry

DESCRIPTIONS = (
    NumberEntityDescription(
        key="night_vision_duration",
        translation_key="night_vision_duration",
        native_min_value=15,
        native_max_value=120,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.SLIDER,
    ),
    NumberEntityDescription(
        key="night_vision_brightness",
        translation_key="night_vision_brightness",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sliders for devices whose night-vision settings were read."""
    del hass
    async_setup_dynamic_entities(
        entry, async_add_entities, lambda snapshot: _entities(entry, snapshot)
    )


def _entities(
    entry: EzvizConfigEntry, snapshot: MetadataSnapshot
) -> Iterable[NumberEntity]:
    return (
        EzvizIlluminationNumber(entry, device, description)
        for device in snapshot.devices
        if device.night_vision is not None
        for description in DESCRIPTIONS
    )


class EzvizIlluminationNumber(EzvizNightVisionEntity, NumberEntity):
    """Represent a verified illumination parameter."""

    entity_description: NumberEntityDescription

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        description: NumberEntityDescription,
    ) -> None:
        self.entity_description = description
        super().__init__(entry, device, description.key)

    @property
    @override
    def native_value(self) -> float | None:
        """Expose the current value as reported by the camera."""
        if (settings := self.settings) is None:
            return None
        if self.entity_description.key == "night_vision_duration":
            return settings.duration
        return settings.brightness

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Change exactly one illumination parameter."""
        if (
            value != int(value)
            or not self.native_min_value <= value <= self.native_max_value
        ):
            raise ValueError("Illumination value out of range")
        if self.entity_description.key == "night_vision_duration":
            await self.async_update_settings(duration=int(value))
        else:
            await self.async_update_settings(brightness=int(value))
