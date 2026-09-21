"""EZVIZ IoT CN integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.client import EzvizApiClient
from .api.image_downloader import create_image_downloader
from .api.scheduler import HubRequestScheduler
from .const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_ALARM_POLLING,
    CONF_MOTION_HOLD_SECONDS,
    DEFAULT_MOTION_HOLD_SECONDS,
    MAX_MOTION_HOLD_SECONDS,
    MIN_MOTION_HOLD_SECONDS,
    PLATFORMS,
)
from .control_evidence import AlarmControlEvidenceRegistry
from .coordinator import (
    AlarmSnapshot,
    EzvizAlarmCoordinator,
    EzvizMetadataCoordinator,
    EzvizStateCoordinator,
)
from .entity import async_register_hub_device
from .event import EventReconciler
from .persistence import EventStateStore
from .runtime import EzvizConfigEntry, EzvizRuntimeData
from .semantics import ObservedEntityRegistry
from .webhook import async_register_native_webhook, async_unregister_native_webhook


def _bounded_int_option(
    entry: EzvizConfigEntry,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Return one defensive integer option within its hard bounds."""
    value = entry.options.get(key, default)
    if isinstance(value, bool):
        value = default
    try:
        parsed = int(value)
    except TypeError, ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


async def async_setup_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:
    """Set up one EZVIZ China Open Platform account."""
    if entry.unique_id is None:
        raise RuntimeError("EZVIZ config entry is missing its stable hub identity")
    session = async_get_clientsession(hass)
    image_downloader = create_image_downloader()
    runtime: EzvizRuntimeData | None = None
    try:
        client = EzvizApiClient(
            session,
            app_key=entry.data[CONF_APP_KEY],
            app_secret=entry.data[CONF_APP_SECRET],
            image_downloader=image_downloader,
        )
        scheduler = HubRequestScheduler()
        observed_registry = ObservedEntityRegistry(entry.unique_id)
        control_evidence = AlarmControlEvidenceRegistry(entry.unique_id)
        reconciler = EventReconciler(
            observed_registry,
            hold_seconds=_bounded_int_option(
                entry,
                CONF_MOTION_HOLD_SECONDS,
                DEFAULT_MOTION_HOLD_SECONDS,
                MIN_MOTION_HOLD_SECONDS,
                MAX_MOTION_HOLD_SECONDS,
            ),
        )
        event_store = EventStateStore(
            hass,
            entry.entry_id,
            reconciler,
            observed_registry,
            control_evidence=control_evidence,
        )
        metadata_coordinator = EzvizMetadataCoordinator(
            hass,
            entry,
            client,
            scheduler,
            control_evidence=control_evidence,
            persist_callback=event_store.async_schedule_save,
        )
        state_coordinator = EzvizStateCoordinator(hass, entry, client, scheduler)
        alarm_coordinator = EzvizAlarmCoordinator(
            hass,
            entry,
            client,
            scheduler,
            reconciler,
            persist_callback=event_store.async_schedule_save,
            included_device_serials=lambda: (
                frozenset(device.serial for device in metadata_coordinator.data.devices)
                if metadata_coordinator.data is not None
                else frozenset()
            ),
        )
        runtime = EzvizRuntimeData(
            client=client,
            scheduler=scheduler,
            reconciler=reconciler,
            observed_registry=observed_registry,
            event_store=event_store,
            metadata_coordinator=metadata_coordinator,
            state_coordinator=state_coordinator,
            alarm_coordinator=alarm_coordinator,
        )
        entry.runtime_data = runtime
        await event_store.async_restore()
        await scheduler.async_start()
        await metadata_coordinator.async_config_entry_first_refresh()
        await state_coordinator.async_config_entry_first_refresh()
        if entry.options.get(CONF_ENABLE_ALARM_POLLING, True):
            await alarm_coordinator.async_config_entry_first_refresh()
        else:
            alarm_coordinator.update_interval = None
            alarm_coordinator.async_set_updated_data(AlarmSnapshot((), 0))
        runtime.hub_device_id = async_register_hub_device(hass, entry)
        await async_register_native_webhook(hass, entry)
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if runtime is None:
            await image_downloader.async_close()
        else:
            try:
                await async_unregister_native_webhook(hass, entry)
            finally:
                await runtime.async_close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> bool:
    """Unload platforms and stop all activity owned by this hub."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> None:
    """Remove private state owned by a deleted config entry."""
    del hass
    await entry.runtime_data.event_store.async_remove()


async def _async_reload_entry(hass: HomeAssistant, entry: EzvizConfigEntry) -> None:
    """Reload the hub after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
