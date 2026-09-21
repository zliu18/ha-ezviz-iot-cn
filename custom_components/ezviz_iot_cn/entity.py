"""Shared entity identity and discovery helpers for EZVIZ IoT CN."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.errors import EzvizError
from .api.models import (
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    channel_uid,
    device_uid,
)
from .api.scheduler import RequestPriority
from .const import (
    CONF_ENABLE_ALARM_POLLING,
    CONF_ENABLE_NATIVE_WEBHOOK,
    DOMAIN,
)
from .coordinator import EzvizStateCoordinator, MetadataSnapshot
from .event import (
    EventReconciler,
    ReconciledAlarmState,
    ReconciledCallingState,
)
from .runtime import EzvizConfigEntry

MANUFACTURER = "EZVIZ"
HUB_MODEL = "China Open Platform"

EntityFactory = Callable[[MetadataSnapshot], Iterable[Entity]]
CapabilityCheck = Callable[[EzvizCapabilities], bool]


def alarm_events_enabled(entry: EzvizConfigEntry) -> bool:
    """Return whether this hub has at least one configured alarm transport."""
    return bool(
        entry.options.get(CONF_ENABLE_ALARM_POLLING, True)
        or entry.options.get(CONF_ENABLE_NATIVE_WEBHOOK, False)
    )


def _hub_id(entry: EzvizConfigEntry) -> str:
    if entry.unique_id is None:
        raise RuntimeError("EZVIZ config entry is missing its stable hub identity")
    return entry.unique_id


def hub_device_info(entry: EzvizConfigEntry) -> DeviceInfo:
    """Return registry information for one Open Platform application hub."""
    return DeviceInfo(
        identifiers={(DOMAIN, _hub_id(entry))},
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
        name=entry.title or "EZVIZ IoT CN",
    )


def physical_device_info(
    entry: EzvizConfigEntry,
    device: EzvizDevice,
    *,
    via_device_id: str | None = None,
) -> DeviceInfo:
    """Return stable hub-scoped registry information for one physical device."""
    hub_id = _hub_id(entry)
    info = DeviceInfo(
        identifiers={(DOMAIN, device_uid(hub_id, device.serial))},
        manufacturer=MANUFACTURER,
        name=device.name,
        serial_number=device.serial,
        sw_version=device.firmware_version,
    )
    if device.model is not None:
        info["model"] = device.model
    if via_device_id is not None:
        info["via_device_id"] = via_device_id
    return info


@callback
def async_register_hub_device(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
) -> str:
    """Register the account hub before child device entities are forwarded."""
    info = hub_device_info(entry)
    return (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers=info["identifiers"],
            manufacturer=info["manufacturer"],
            model=info["model"],
            name=info["name"],
        )
        .id
    )


@callback
def async_setup_dynamic_entities(
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: EntityFactory,
) -> None:
    """Add newly discovered entities once while preserving registry entries."""
    coordinator = entry.runtime_data.metadata_coordinator
    known_unique_ids: set[str] = set()

    @callback
    def discover() -> None:
        entities = tuple(factory(coordinator.data))
        new_entities = [
            entity
            for entity in entities
            if entity.unique_id is not None and entity.unique_id not in known_unique_ids
        ]
        if not new_entities:
            return
        known_unique_ids.update(
            entity.unique_id for entity in new_entities if entity.unique_id is not None
        )
        async_add_entities(new_entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
    entry.async_on_unload(entry.runtime_data.observed_registry.subscribe(discover))


class EzvizDeviceEntity(CoordinatorEntity[EzvizStateCoordinator]):
    """Base entity attached to one stable physical EZVIZ device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        key: str,
        *,
        capability_check: CapabilityCheck | None = None,
    ) -> None:
        super().__init__(entry.runtime_data.state_coordinator)
        self._metadata_coordinator = entry.runtime_data.metadata_coordinator
        self._capability_check = capability_check
        self._device_serial = device.serial
        self._attr_device_info = physical_device_info(
            entry,
            device,
            via_device_id=entry.runtime_data.hub_device_id,
        )
        self._device_id = device_uid(_hub_id(entry), device.serial)
        self._attr_unique_id = f"{self._device_id}:{key}"

    @property
    def current_device(self) -> EzvizDevice | None:
        """Return current device state, or None when absent from the snapshot."""
        return next(
            (
                device
                for device in self.coordinator.data.devices
                if device.serial == self._device_serial
            ),
            None,
        )

    @property
    def current_capabilities(self) -> EzvizCapabilities | None:
        """Return current normalized capabilities for this physical device."""
        return next(
            (
                capabilities
                for capabilities in self._metadata_coordinator.data.capabilities
                if capabilities.device_serial == self._device_serial
            ),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Remain registered but unavailable while the device is absent."""
        if not super().available or self.current_device is None:
            return False
        if self._capability_check is None:
            return True
        capabilities = self.current_capabilities
        return (
            self._metadata_coordinator.last_update_success
            and capabilities is not None
            and self._capability_check(capabilities)
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Refresh availability when slow-changing metadata changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._metadata_coordinator.async_add_listener(self._handle_metadata_update)
        )

    @callback
    def _handle_metadata_update(self) -> None:
        self.async_write_ha_state()


class EzvizChannelEntity(EzvizDeviceEntity):
    """Base entity for one channel while retaining physical-device grouping."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        key: str,
        *,
        capability_check: CapabilityCheck | None = None,
    ) -> None:
        super().__init__(
            entry,
            device,
            key,
            capability_check=capability_check,
        )
        self._channel_no = channel.channel_no
        self._attr_unique_id = (
            f"{channel_uid(self._device_id, channel.channel_no)}:{key}"
        )
        channel_count = sum(
            item.device_serial == channel.device_serial
            for item in self._metadata_coordinator.data.channels
        )
        if channel_count > 1:
            self._attr_translation_key = f"{key}_channel"
            self._attr_translation_placeholders = {"channel": channel.name}
        else:
            self._attr_translation_key = key
            self._attr_translation_placeholders = {}

    @property
    def current_channel(self) -> EzvizChannel | None:
        """Return current channel metadata, or None after channel removal."""
        return next(
            (
                channel
                for channel in self._metadata_coordinator.data.channels
                if channel.device_serial == self._device_serial
                and channel.channel_no == self._channel_no
            ),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Remain registered but unavailable while the channel is absent."""
        return (
            super().available
            and self._metadata_coordinator.last_update_success
            and self.current_channel is not None
        )


class EzvizAlarmEntity(EzvizChannelEntity):
    """Channel entity backed by the shared Push/poll alarm reconciler."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        key: str,
        *,
        capability_check: CapabilityCheck | None = None,
    ) -> None:
        super().__init__(
            entry,
            device,
            channel,
            key,
            capability_check=capability_check,
        )
        self._reconciler: EventReconciler = entry.runtime_data.reconciler

    @property
    def alarm_state(self) -> ReconciledAlarmState | None:
        """Return current reconciled alarm state for this channel."""
        return self._reconciler.alarm_state_for(
            self._device_serial,
            self._channel_no,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to semantic alarm updates while the entity is loaded."""
        await super().async_added_to_hass()
        self.async_on_remove(self._reconciler.subscribe(self._handle_alarm_update))

    @callback
    def _handle_alarm_update(self, state: object) -> None:
        if (
            isinstance(state, ReconciledAlarmState)
            and state.device_serial == self._device_serial
            and state.channel_no == self._channel_no
        ):
            self.async_write_ha_state()


class EzvizCallingEntity(EzvizChannelEntity):
    """Channel entity backed by the shared calling-event reconciler."""

    def __init__(
        self,
        entry: EzvizConfigEntry,
        device: EzvizDevice,
        channel: EzvizChannel,
        key: str,
    ) -> None:
        super().__init__(entry, device, channel, key)
        self._reconciler: EventReconciler = entry.runtime_data.reconciler

    @property
    def calling_state(self) -> ReconciledCallingState | None:
        """Return current reconciled calling state for this channel."""
        return self._reconciler.calling_state_for(
            self._device_serial,
            self._channel_no,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to calling-state updates while the entity is loaded."""
        await super().async_added_to_hass()
        self.async_on_remove(self._reconciler.subscribe(self._handle_calling_update))

    @callback
    def _handle_calling_update(self, state: object) -> None:
        if (
            isinstance(state, ReconciledCallingState)
            and state.device_serial == self._device_serial
            and state.channel_no == self._channel_no
        ):
            self.async_write_ha_state()


def devices_by_serial(snapshot: MetadataSnapshot) -> dict[str, EzvizDevice]:
    """Index normalized devices for platform entity factories."""
    return {device.serial: device for device in snapshot.devices}


def capabilities_by_serial(snapshot: MetadataSnapshot) -> dict[str, Any]:
    """Index normalized capabilities for platform entity factories."""
    return {
        capabilities.device_serial: capabilities
        for capabilities in snapshot.capabilities
    }


async def async_execute_control(
    entry: EzvizConfigEntry,
    operation: Callable[[], Awaitable[None]],
    *,
    action: str,
) -> None:
    """Run one non-coalesced user control and normalize API failures for HA."""
    try:
        await entry.runtime_data.scheduler.async_submit(
            operation,
            priority=RequestPriority.USER_CONTROL,
        )
    except EzvizError as err:
        raise HomeAssistantError(f"Unable to {action}") from err
