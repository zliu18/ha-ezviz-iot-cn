"""Tests for config-entry setup and unload lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ezviz_iot_cn.api.client import EzvizApiClient
from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    AlarmSoundMode,
    EzvizAlarmLightState,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    hub_uid,
)
from custom_components.ezviz_iot_cn.api.usage import ApiUsageTracker
from custom_components.ezviz_iot_cn.const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_ALARM_POLLING,
    CONF_MOTION_HOLD_SECONDS,
    DOMAIN,
    PLATFORMS,
)
from custom_components.ezviz_iot_cn.coordinator import AlarmSnapshot
from custom_components.ezviz_iot_cn.runtime import (
    EzvizImageCache,
    EzvizRuntimeData,
    ImageCacheKey,
    ImageKind,
)


def _entry(
    *,
    options: dict | None = None,
    state: ConfigEntryState = ConfigEntryState.SETUP_IN_PROGRESS,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APP_KEY: "synthetic-key",
            CONF_APP_SECRET: "synthetic-secret",
        },
        options=options or {},
        state=state,
        unique_id=hub_uid("synthetic-key"),
    )


def test_platforms_are_explicit_and_complete() -> None:
    """The hub forwards exactly the requested capability-driven platforms."""
    assert PLATFORMS == (
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.IMAGE,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SENSOR,
        Platform.SWITCH,
    )


@pytest.mark.asyncio
async def test_manual_device_refresh_is_hub_cooled_and_includes_alarms() -> None:
    """All device buttons share one bounded account-level refresh path."""
    coordinators = [Mock() for _ in range(3)]
    for coordinator in coordinators:
        coordinator.async_request_refresh = AsyncMock()
        coordinator.last_update_success = True
    runtime = EzvizRuntimeData(
        client=Mock(),
        scheduler=Mock(),
        reconciler=Mock(),
        observed_registry=Mock(),
        event_store=Mock(),
        metadata_coordinator=coordinators[2],
        state_coordinator=coordinators[0],
        alarm_coordinator=coordinators[1],
    )

    await runtime.async_refresh_device_state()
    for coordinator in coordinators:
        coordinator.async_request_refresh.assert_awaited_once_with()
    with pytest.raises(HomeAssistantError, match="once per minute"):
        await runtime.async_refresh_device_state()
    for coordinator in coordinators:
        coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_setup_uses_shared_session_refreshes_then_forwards(hass) -> None:
    """Setup injects HA networking and publishes runtime only after first data."""
    entry = _entry()
    entry.add_to_hass(hass)
    session = Mock(name="shared_home_assistant_session")
    image_downloader = Mock(name="owned_image_downloader")
    forward = AsyncMock()
    hass.config_entries.async_forward_entry_setups = forward

    with (
        patch(
            "custom_components.ezviz_iot_cn.async_get_clientsession",
            return_value=session,
        ) as get_session,
        patch(
            "custom_components.ezviz_iot_cn.create_image_downloader",
            return_value=image_downloader,
        ) as create_downloader,
        patch("custom_components.ezviz_iot_cn.EzvizApiClient") as client_class,
        patch(
            "custom_components.ezviz_iot_cn.EzvizMetadataCoordinator"
        ) as metadata_class,
        patch("custom_components.ezviz_iot_cn.EzvizStateCoordinator") as state_class,
        patch("custom_components.ezviz_iot_cn.EzvizAlarmCoordinator") as alarm_class,
    ):
        client_class.return_value.async_close = AsyncMock()
        metadata_class.return_value.async_config_entry_first_refresh = AsyncMock()
        metadata_class.return_value.data = None
        state_class.return_value.async_config_entry_first_refresh = AsyncMock()
        alarm_class.return_value.async_config_entry_first_refresh = AsyncMock()
        metadata_class.return_value.async_shutdown = AsyncMock()
        state_class.return_value.async_shutdown = AsyncMock()
        alarm_class.return_value.async_shutdown = AsyncMock()

        assert await async_setup_entry(hass, entry) is True

    get_session.assert_called_once_with(hass)
    create_downloader.assert_called_once_with()
    client_class.assert_called_once_with(
        session,
        app_key="synthetic-key",
        app_secret="synthetic-secret",
        image_downloader=image_downloader,
    )
    metadata_class.return_value.async_config_entry_first_refresh.assert_awaited_once()
    state_class.return_value.async_config_entry_first_refresh.assert_awaited_once()
    alarm_class.return_value.async_config_entry_first_refresh.assert_awaited_once()
    included_device_serials = alarm_class.call_args.kwargs["included_device_serials"]
    assert included_device_serials() == frozenset()
    forward.assert_awaited_once_with(entry, PLATFORMS)
    assert isinstance(entry.runtime_data, EzvizRuntimeData)

    await entry.runtime_data.async_close()


@pytest.mark.asyncio
async def test_full_setup_registers_verified_controls_and_hub_diagnostics(
    hass,
    enable_custom_integrations,
) -> None:
    """HA forwarding registers controls and local counters under their devices."""
    del enable_custom_integrations
    serial = "SYNTHETIC-SERIAL-001"
    device = EzvizDevice(
        serial=serial,
        name="Courtyard",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=None,
        firmware_version="V1.0.0",
    )
    channel = EzvizChannel(serial, 1, "Courtyard", True, False)
    capabilities = EzvizCapabilities(
        serial,
        True,
        True,
        False,
        False,
        False,
        alarm_voice_level=1,
        active_defense_level=2,
    )
    client = AsyncMock(spec=EzvizApiClient)
    client.async_list_channels.return_value = (channel,)
    client.async_list_devices.return_value = (device,)
    client.async_get_capabilities.return_value = capabilities
    client.async_get_alarm_sound_mode.return_value = AlarmSoundMode.SILENT
    client.async_get_alarm_light_state.return_value = EzvizAlarmLightState(
        AlarmLightProtocol.MOTION, True
    )
    client.usage_tracker = ApiUsageTracker()
    client.usage_snapshot = client.usage_tracker.snapshot
    client.async_close = AsyncMock()
    entry = _entry(
        options={CONF_ENABLE_ALARM_POLLING: False},
        state=ConfigEntryState.NOT_LOADED,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezviz_iot_cn.EzvizApiClient",
            return_value=client,
        ),
        patch("custom_components.ezviz_iot_cn.create_image_downloader"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    alarm_sound_entries = [
        item
        for item in entries
        if item.domain == Platform.SELECT
        and item.unique_id.endswith(":alarm_sound_mode")
    ]
    assert len(alarm_sound_entries) == 1
    state = hass.states.get(alarm_sound_entries[0].entity_id)
    assert state is not None
    assert state.state == "silent"
    alarm_flash = next(
        item
        for item in entries
        if item.domain == Platform.SWITCH and item.unique_id.endswith(":alarm_flash")
    )
    assert hass.states.get(alarm_flash.entity_id).state == "on"
    assert any(
        item.domain == Platform.BUTTON
        and item.unique_id.endswith(":refresh_device_state")
        for item in entries
    )
    assert any(
        item.domain == Platform.SENSOR
        and item.unique_id.endswith(":local_api_requests")
        and hass.states.get(item.entity_id).state == "0"
        for item in entries
    )
    client.async_get_alarm_light_state.assert_awaited_once_with(
        serial, channel_no=1, protocol=AlarmLightProtocol.MOTION
    )

    assert await hass.config_entries.async_unload(entry.entry_id) is True


@pytest.mark.asyncio
async def test_setup_failure_closes_owned_runtime(hass) -> None:
    """A failed first refresh cannot leave workers or timers running."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ezviz_iot_cn.async_get_clientsession"),
        patch("custom_components.ezviz_iot_cn.create_image_downloader"),
        patch("custom_components.ezviz_iot_cn.HubRequestScheduler") as scheduler_class,
        patch(
            "custom_components.ezviz_iot_cn.EzvizMetadataCoordinator"
        ) as metadata_class,
        patch("custom_components.ezviz_iot_cn.EzvizRuntimeData") as runtime_class,
    ):
        scheduler_class.return_value.async_start = AsyncMock()
        metadata_class.return_value.async_config_entry_first_refresh = AsyncMock(
            side_effect=RuntimeError("synthetic setup failure")
        )
        metadata_class.return_value.async_shutdown = AsyncMock()
        runtime_class.return_value.async_close = AsyncMock()

        with pytest.raises(RuntimeError, match="synthetic setup failure"):
            await async_setup_entry(hass, entry)

    runtime_class.return_value.async_close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_setup_failure_before_runtime_closes_image_downloader(hass) -> None:
    """An early constructor failure cannot leak the dedicated image session."""
    entry = _entry()
    entry.add_to_hass(hass)
    image_downloader = Mock()
    image_downloader.async_close = AsyncMock()

    with (
        patch("custom_components.ezviz_iot_cn.async_get_clientsession"),
        patch(
            "custom_components.ezviz_iot_cn.create_image_downloader",
            return_value=image_downloader,
        ),
        patch(
            "custom_components.ezviz_iot_cn.EzvizApiClient",
            side_effect=RuntimeError("synthetic constructor failure"),
        ),
        pytest.raises(RuntimeError, match="synthetic constructor failure"),
    ):
        await async_setup_entry(hass, entry)

    image_downloader.async_close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_setup_honors_disabled_alarm_polling_and_motion_hold(hass) -> None:
    """Options control alarm scheduling and reconciler transient-state timing."""
    entry = _entry(
        options={
            CONF_ENABLE_ALARM_POLLING: False,
            CONF_MOTION_HOLD_SECONDS: 45,
        }
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    image_downloader = Mock()
    image_downloader.async_close = AsyncMock()

    with (
        patch("custom_components.ezviz_iot_cn.async_get_clientsession"),
        patch(
            "custom_components.ezviz_iot_cn.create_image_downloader",
            return_value=image_downloader,
        ),
        patch(
            "custom_components.ezviz_iot_cn.ObservedEntityRegistry"
        ) as observed_registry,
        patch("custom_components.ezviz_iot_cn.EventReconciler") as reconciler,
        patch(
            "custom_components.ezviz_iot_cn.EzvizMetadataCoordinator"
        ) as metadata_class,
        patch("custom_components.ezviz_iot_cn.EzvizStateCoordinator") as state_class,
        patch("custom_components.ezviz_iot_cn.EzvizAlarmCoordinator") as alarm_class,
    ):
        metadata_class.return_value.async_config_entry_first_refresh = AsyncMock()
        state_class.return_value.async_config_entry_first_refresh = AsyncMock()
        alarm_class.return_value.async_config_entry_first_refresh = AsyncMock()
        metadata_class.return_value.async_shutdown = AsyncMock()
        state_class.return_value.async_shutdown = AsyncMock()
        alarm_class.return_value.async_shutdown = AsyncMock()
        reconciler.return_value.export_state.return_value = {
            "version": 2,
            "cursor": None,
            "recent": [],
        }
        observed_registry.return_value.export_state.return_value = {
            "version": 1,
            "channels": {},
        }
        reconciler.return_value.async_close = AsyncMock()

        assert await async_setup_entry(hass, entry) is True

    reconciler.assert_called_once_with(
        observed_registry.return_value,
        hold_seconds=45,
    )
    alarm_class.return_value.async_config_entry_first_refresh.assert_not_awaited()
    alarm_class.return_value.async_set_updated_data.assert_called_once_with(
        AlarmSnapshot((), 0)
    )

    await entry.runtime_data.async_close()


@pytest.mark.asyncio
async def test_unload_stops_all_owned_activity_and_is_idempotent(hass) -> None:
    """Platform unload is followed by finite runtime cleanup on every call."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = Mock(spec=EzvizRuntimeData)
    runtime.async_close = AsyncMock()
    entry.runtime_data = runtime
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True
    assert await async_unload_entry(hass, entry) is True

    assert hass.config_entries.async_unload_platforms.await_count == 2
    assert runtime.async_close.await_count == 2


@pytest.mark.asyncio
async def test_failed_platform_unload_preserves_live_runtime(hass) -> None:
    """Runtime remains available if a platform refuses to unload."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = Mock(spec=EzvizRuntimeData)
    runtime.async_close = AsyncMock()
    entry.runtime_data = runtime
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    assert await async_unload_entry(hass, entry) is False

    runtime.async_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_entry_deletes_private_event_state(hass) -> None:
    """Config-entry removal deletes the integration-owned private Store."""
    entry = _entry()
    event_store = Mock()
    event_store.async_remove = AsyncMock()
    entry.runtime_data = Mock(spec=EzvizRuntimeData, event_store=event_store)

    await async_remove_entry(hass, entry)

    event_store.async_remove.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_close_cancels_ptz_before_scheduler_shutdown() -> None:
    """Unload lets PTZ cancellation cleanup finish before closing its scheduler."""
    order: list[str] = []
    capture_started = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()

    async def capturing() -> None:
        capture_started.set()
        try:
            await release.wait()
        finally:
            order.append("capture_cleanup")

    async def moving() -> None:
        started.set()
        try:
            await release.wait()
        finally:
            order.append("ptz_cleanup")

    capture_task = asyncio.create_task(capturing())
    task = asyncio.create_task(moving())
    await capture_started.wait()
    await started.wait()
    scheduler = Mock()
    scheduler.async_close = AsyncMock(side_effect=lambda: order.append("scheduler"))
    reconciler = Mock()
    reconciler.async_close = AsyncMock()
    event_store = Mock()
    event_store.async_flush = AsyncMock()
    observed_registry = Mock()
    metadata = Mock()
    metadata.async_shutdown = AsyncMock()
    state = Mock()
    state.async_shutdown = AsyncMock()
    alarm = Mock()
    alarm.async_shutdown = AsyncMock()
    image_cache = EzvizImageCache()
    image_cache.set(
        ImageCacheKey("synthetic-device", 1, ImageKind.CAPTURE),
        b"\xff\xd8\xff\xe0synthetic",
        "image/jpeg",
        datetime(2026, 9, 17, 12, tzinfo=UTC),
    )
    client = Mock()
    client.async_close = AsyncMock(side_effect=lambda: order.append("client"))
    runtime = EzvizRuntimeData(
        client=client,
        scheduler=scheduler,
        reconciler=reconciler,
        observed_registry=observed_registry,
        event_store=event_store,
        metadata_coordinator=metadata,
        state_coordinator=state,
        alarm_coordinator=alarm,
        image_cache=image_cache,
        capture_task=capture_task,
        ptz_tasks={("synthetic-device", 1): task},
        ptz_locks={("synthetic-device", 1): asyncio.Lock()},
    )

    await runtime.async_close()

    assert task.cancelled()
    assert capture_task.cancelled()
    assert order == ["capture_cleanup", "ptz_cleanup", "scheduler", "client"]
    client.async_close.assert_awaited_once_with()
    assert runtime.capture_task is None
    assert runtime.image_cache.count == 0
    assert runtime.ptz_tasks == {}
    assert runtime.ptz_locks == {}


@pytest.mark.asyncio
async def test_runtime_close_still_closes_client_after_cleanup_failure() -> None:
    """Earlier cleanup errors cannot leak the dedicated image session."""
    client = Mock()
    client.async_close = AsyncMock()
    scheduler = Mock()
    scheduler.async_close = AsyncMock()
    reconciler = Mock()
    reconciler.async_close = AsyncMock()
    event_store = Mock()
    event_store.async_flush = AsyncMock(
        side_effect=RuntimeError("synthetic persistence failure")
    )
    metadata = Mock()
    metadata.async_shutdown = AsyncMock()
    state = Mock()
    state.async_shutdown = AsyncMock()
    alarm = Mock()
    alarm.async_shutdown = AsyncMock()
    runtime = EzvizRuntimeData(
        client=client,
        scheduler=scheduler,
        reconciler=reconciler,
        observed_registry=Mock(),
        event_store=event_store,
        metadata_coordinator=metadata,
        state_coordinator=state,
        alarm_coordinator=alarm,
    )

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        await runtime.async_close()

    client.async_close.assert_awaited_once_with()
