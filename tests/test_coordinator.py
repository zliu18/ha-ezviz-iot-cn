"""Tests for Home Assistant coordinators and account-level batching."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, call

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn.api.errors import (
    EzvizAuthError,
    EzvizConnectionError,
    EzvizNotSupportedError,
    EzvizProtocolError,
    EzvizRateLimitError,
)
from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    AlarmSoundMode,
    EventSource,
    EzvizAlarmEvent,
    EzvizAlarmLightState,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    hub_uid,
)
from custom_components.ezviz_iot_cn.api.scheduler import HubRequestScheduler
from custom_components.ezviz_iot_cn.const import (
    CONF_ALARM_INTERVAL,
    CONF_DEVICE_FILTER_MODE,
    CONF_INCLUDE_SHARED_DEVICES,
    CONF_METADATA_INTERVAL,
    CONF_SELECTED_DEVICES,
    CONF_STATE_INTERVAL,
    DOMAIN,
    FILTER_MODE_EXCLUDE,
)
from custom_components.ezviz_iot_cn.control_evidence import AlarmControlEvidenceRegistry
from custom_components.ezviz_iot_cn.coordinator import (
    EzvizAlarmCoordinator,
    EzvizMetadataCoordinator,
    EzvizStateCoordinator,
)
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.semantics import ObservedEntityRegistry

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"


def _device(
    serial: str,
    *,
    alarm_sound_mode: AlarmSoundMode | None = AlarmSoundMode.SILENT,
    is_shared: bool = False,
) -> EzvizDevice:
    return EzvizDevice(
        serial=serial,
        name=f"Camera {serial[-1]}",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=alarm_sound_mode,
        firmware_version="V1.0.0",
        is_shared=is_shared,
    )


def _channel(serial: str) -> EzvizChannel:
    return EzvizChannel(
        device_serial=serial,
        channel_no=1,
        name=f"Channel {serial[-1]}",
        online=True,
        encrypted=True,
    )


def _capabilities(serial: str) -> EzvizCapabilities:
    return EzvizCapabilities(
        device_serial=serial,
        supports_capture=True,
        supports_defence=True,
        supports_ptz=True,
        ptz_top_bottom=True,
        ptz_left_right=True,
        alarm_voice_level=1,
    )


def _entry(*, options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"app_key": "synthetic-key", "app_secret": "synthetic-secret"},
        options=options or {},
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        unique_id="synthetic-hub-id",
    )


@pytest.mark.asyncio
async def test_metadata_refresh_batches_account_lists_and_selected_capabilities(
    hass,
) -> None:
    """Metadata uses two account calls and one bounded capability call per selection."""
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1), _device(SERIAL_2))
    client.async_list_channels.return_value = (_channel(SERIAL_1), _channel(SERIAL_2))
    client.async_get_capabilities.side_effect = lambda serial: _capabilities(serial)
    client.async_get_alarm_sound_mode.side_effect = lambda _serial: (
        AlarmSoundMode.SILENT
    )
    scheduler = HubRequestScheduler(worker_count=2, max_queue_size=8)
    entry = _entry(
        options={
            CONF_SELECTED_DEVICES: [SERIAL_2],
            CONF_METADATA_INTERVAL: 7200,
        }
    )
    coordinator = EzvizMetadataCoordinator(hass, entry, client, scheduler)

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.data.devices == (_device(SERIAL_2),)
    assert coordinator.data.channels == (_channel(SERIAL_2),)
    assert coordinator.data.capabilities == (_capabilities(SERIAL_2),)
    assert coordinator.update_interval == timedelta(seconds=7200)
    client.async_list_channels.assert_awaited_once_with(include_shared_devices=True)
    client.async_list_devices.assert_awaited_once_with(
        include_shared_devices=True,
        channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
    )
    client.async_get_capabilities.assert_awaited_once_with(SERIAL_2)
    client.async_get_alarm_sound_mode.assert_awaited_once_with(SERIAL_2)
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_metadata_uses_status_for_alarm_sound_and_skips_shared_devices(
    hass,
) -> None:
    """Missing list fields are enriched only for directly controlled devices."""
    direct = _device(SERIAL_1, alarm_sound_mode=None)
    shared = _device(SERIAL_2, alarm_sound_mode=None, is_shared=True)
    client = AsyncMock()
    client.async_list_devices.return_value = (direct, shared)
    client.async_list_channels.return_value = (_channel(SERIAL_1), _channel(SERIAL_2))
    client.async_get_capabilities.side_effect = lambda serial: _capabilities(serial)
    client.async_get_alarm_sound_mode.return_value = AlarmSoundMode.SHORT_ALERT
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizMetadataCoordinator(hass, _entry(), client, scheduler)

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.data.devices[0].alarm_sound_mode is AlarmSoundMode.SHORT_ALERT
    assert coordinator.data.devices[1].alarm_sound_mode is None
    client.async_get_alarm_sound_mode.assert_awaited_once_with(SERIAL_1)
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_controls_require_independent_capability_and_verified_status(
    hass,
) -> None:
    """Sound, flash-only, and unverified-flash devices share one generic path."""
    serial_3 = "SYNTHETIC-SERIAL-003"
    client = AsyncMock()
    client.async_list_devices.return_value = (
        _device(SERIAL_1),
        _device(SERIAL_2),
        _device(serial_3),
    )
    client.async_list_channels.return_value = tuple(
        _channel(serial) for serial in (SERIAL_1, SERIAL_2, serial_3)
    )
    levels = {
        SERIAL_1: (1, 1),
        SERIAL_2: (0, 2),
        serial_3: (3, 2),
    }
    client.async_get_capabilities.side_effect = lambda serial: replace(
        _capabilities(serial),
        alarm_voice_level=levels[serial][0],
        active_defense_level=levels[serial][1],
    )
    client.async_get_alarm_sound_mode.return_value = AlarmSoundMode.SILENT

    async def light_state(serial, *, channel_no, protocol):
        assert channel_no == 1
        assert serial != SERIAL_1
        if serial == SERIAL_2 and protocol is AlarmLightProtocol.MOTION:
            return EzvizAlarmLightState(protocol, True)
        raise EzvizNotSupportedError("public status unavailable")

    client.async_get_alarm_light_state.side_effect = light_state
    scheduler = HubRequestScheduler(worker_count=1)
    evidence = AlarmControlEvidenceRegistry(hub_uid("synthetic-key"))
    persist = Mock()
    coordinator = EzvizMetadataCoordinator(
        hass,
        _entry(),
        client,
        scheduler,
        control_evidence=evidence,
        persist_callback=persist,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
        devices = {device.serial: device for device in coordinator.data.devices}
        assert devices[SERIAL_1].alarm_sound_mode is AlarmSoundMode.SILENT
        assert devices[SERIAL_1].alarm_light_protocol is None
        assert devices[SERIAL_2].alarm_sound_mode is None
        assert devices[SERIAL_2].alarm_light_enabled is True
        assert devices[SERIAL_2].alarm_light_protocol is AlarmLightProtocol.MOTION
        assert devices[serial_3].alarm_sound_mode is AlarmSoundMode.SILENT
        assert devices[serial_3].alarm_light_protocol is None
        assert coordinator.data.alarm_light_probe_reasons == (
            "light_capable_but_public_status_unsupported",
        )
        assert client.async_get_alarm_sound_mode.await_count == 2
        assert client.async_get_alarm_light_state.await_count == 3
        assert evidence.light_protocol_for(SERIAL_2) is AlarmLightProtocol.MOTION
        assert SERIAL_2 not in str(evidence.export_state())
        persist.assert_called_once_with()
    finally:
        await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_light_tries_other_protocol_and_retains_evidence_when_offline(
    hass,
) -> None:
    """A false success cannot verify 301; offline does not remove proven 305."""
    client = AsyncMock()
    device = _device(SERIAL_1)
    client.async_list_devices.return_value = (device,)
    client.async_list_channels.return_value = (_channel(SERIAL_1),)
    client.async_get_capabilities.return_value = replace(
        _capabilities(SERIAL_1), active_defense_level=2
    )
    client.async_get_alarm_sound_mode.return_value = AlarmSoundMode.SILENT
    client.async_get_alarm_light_state.side_effect = [
        EzvizProtocolError("data.type did not match 301"),
        EzvizAlarmLightState(AlarmLightProtocol.PIR, False),
    ]
    scheduler = HubRequestScheduler(worker_count=1)
    evidence = AlarmControlEvidenceRegistry(hub_uid("synthetic-key"))
    coordinator = EzvizMetadataCoordinator(
        hass, _entry(), client, scheduler, control_evidence=evidence
    )
    try:
        await coordinator.async_config_entry_first_refresh()
        assert (
            coordinator.data.devices[0].alarm_light_protocol is AlarmLightProtocol.PIR
        )
        assert coordinator.data.devices[0].alarm_light_enabled is False
        protocols = [
            call.kwargs["protocol"]
            for call in client.async_get_alarm_light_state.await_args_list
        ]
        assert protocols == [AlarmLightProtocol.MOTION, AlarmLightProtocol.PIR]

        restored = AlarmControlEvidenceRegistry(hub_uid("synthetic-key"))
        restored.restore_state(evidence.export_state())
        client.async_list_devices.return_value = (replace(device, online=False),)
        offline = EzvizMetadataCoordinator(
            hass, _entry(), client, scheduler, control_evidence=restored
        )
        await offline.async_config_entry_first_refresh()
        assert offline.data.devices[0].alarm_light_protocol is AlarmLightProtocol.PIR
        assert offline.data.devices[0].alarm_light_enabled is None
        assert client.async_get_alarm_light_state.await_count == 2
        await offline.async_shutdown()
    finally:
        await scheduler.async_close()


@pytest.mark.asyncio
async def test_runtime_refreshes_respect_shared_device_option(hass) -> None:
    """Ignoring the saved option would reload shared devices after setup."""
    client = AsyncMock()
    client.async_list_channels.return_value = ()
    client.async_list_devices.return_value = ()
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    entry = _entry(options={CONF_INCLUDE_SHARED_DEVICES: False})
    metadata = EzvizMetadataCoordinator(hass, entry, client, scheduler)
    state = EzvizStateCoordinator(hass, entry, client, scheduler)

    try:
        await metadata.async_config_entry_first_refresh()
        await state.async_config_entry_first_refresh()

        assert metadata.data.devices == ()
        assert metadata.data.channels == ()
        assert state.data.devices == ()
        client.async_list_channels.assert_awaited_once_with(
            include_shared_devices=False
        )
        client.async_list_devices.assert_any_await(
            include_shared_devices=False,
            channels=(),
        )
        client.async_list_devices.assert_any_await(include_shared_devices=False)
    finally:
        await scheduler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_error",
    [
        EzvizNotSupportedError("unsupported"),
        EzvizConnectionError("offline"),
    ],
)
async def test_metadata_keeps_devices_when_one_capability_request_is_isolated(
    hass,
    device_error: Exception,
) -> None:
    """One device-scoped capability failure must not hide account metadata."""
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1), _device(SERIAL_2))
    client.async_list_channels.return_value = (_channel(SERIAL_1), _channel(SERIAL_2))
    client.async_get_capabilities.side_effect = [
        _capabilities(SERIAL_1),
        device_error,
    ]
    client.async_get_alarm_sound_mode.side_effect = lambda _serial: (
        AlarmSoundMode.SILENT
    )
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizMetadataCoordinator(hass, _entry(), client, scheduler)

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.data.devices == (
        _device(SERIAL_1),
        _device(SERIAL_2, alarm_sound_mode=None),
    )
    assert coordinator.data.channels == (_channel(SERIAL_1), _channel(SERIAL_2))
    assert coordinator.data.capabilities == (_capabilities(SERIAL_1),)
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_metadata_fails_when_no_capability_succeeds_and_one_is_offline(
    hass,
) -> None:
    """An entirely unreachable capability snapshot must not look healthy."""
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1), _device(SERIAL_2))
    client.async_list_channels.return_value = (_channel(SERIAL_1), _channel(SERIAL_2))
    client.async_get_capabilities.side_effect = [
        EzvizNotSupportedError("unsupported"),
        EzvizConnectionError("offline"),
    ]
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizMetadataCoordinator(hass, _entry(), client, scheduler)

    with pytest.raises(UpdateFailed, match="EZVIZ metadata update failed"):
        await coordinator._async_update_data()

    await scheduler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_error", "expected_error"),
    [
        (EzvizAuthError("rejected"), ConfigEntryAuthFailed),
        (EzvizRateLimitError("limited"), UpdateFailed),
        (EzvizProtocolError("invalid"), UpdateFailed),
    ],
)
async def test_metadata_keeps_fatal_capability_errors_fatal(
    hass,
    device_error: Exception,
    expected_error: type[Exception],
) -> None:
    """Account and contract failures must not become partial success."""
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1),)
    client.async_list_channels.return_value = (_channel(SERIAL_1),)
    client.async_get_capabilities.side_effect = device_error
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizMetadataCoordinator(hass, _entry(), client, scheduler)

    with pytest.raises(expected_error):
        await coordinator._async_update_data()

    await scheduler.async_close()


@pytest.mark.asyncio
async def test_state_refresh_is_one_account_request_and_honors_interval(hass) -> None:
    """State polling is account-level and filters locally for selected devices."""
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1), _device(SERIAL_2))
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    entry = _entry(
        options={
            CONF_SELECTED_DEVICES: [SERIAL_1],
            CONF_STATE_INTERVAL: 90,
        }
    )
    coordinator = EzvizStateCoordinator(hass, entry, client, scheduler)

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.update_interval == timedelta(seconds=90)
    assert coordinator.data.devices == (_device(SERIAL_1),)
    client.async_list_devices.assert_awaited_once_with(include_shared_devices=True)
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_explicit_exclude_mode_filters_metadata_state_and_alarms(hass) -> None:
    """Every polling boundary applies the same denylist semantics."""
    selected_alarm = EzvizAlarmEvent(
        alarm_id="alarm-selected",
        device_serial=SERIAL_2,
        channel_no=1,
        alarm_type="motion",
        occurred_at=NOW,
        image_url=None,
        source=EventSource.POLL,
    )
    excluded_alarm = EzvizAlarmEvent(
        alarm_id="alarm-excluded",
        device_serial=SERIAL_1,
        channel_no=1,
        alarm_type="motion",
        occurred_at=NOW,
        image_url=None,
        source=EventSource.POLL,
    )
    client = AsyncMock()
    client.async_list_devices.return_value = (_device(SERIAL_1), _device(SERIAL_2))
    client.async_list_channels.return_value = (_channel(SERIAL_1), _channel(SERIAL_2))
    client.async_get_capabilities.side_effect = lambda serial: _capabilities(serial)
    client.async_get_alarm_sound_mode.side_effect = lambda _serial: (
        AlarmSoundMode.SILENT
    )
    client.async_list_alarms.return_value = (excluded_alarm, selected_alarm)
    scheduler = HubRequestScheduler(worker_count=2, max_queue_size=8)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=lambda: NOW, hold_seconds=60)
    entry = _entry(
        options={
            CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
            CONF_SELECTED_DEVICES: [SERIAL_1],
        }
    )
    metadata = EzvizMetadataCoordinator(hass, entry, client, scheduler)
    state = EzvizStateCoordinator(hass, entry, client, scheduler)
    alarms = EzvizAlarmCoordinator(
        hass,
        entry,
        client,
        scheduler,
        reconciler,
        now=lambda: NOW,
    )

    await metadata.async_config_entry_first_refresh()
    await state.async_config_entry_first_refresh()
    await alarms.async_config_entry_first_refresh()

    assert metadata.data.devices == (_device(SERIAL_2),)
    assert metadata.data.channels == (_channel(SERIAL_2),)
    assert metadata.data.capabilities == (_capabilities(SERIAL_2),)
    assert state.data.devices == (_device(SERIAL_2),)
    assert alarms.data.events == (selected_alarm,)
    assert reconciler.alarm_state_for(SERIAL_1, 1) is None
    assert reconciler.alarm_state_for(SERIAL_2, 1) is not None
    client.async_list_alarms.assert_awaited_once_with(
        start_time=NOW - timedelta(hours=24),
        end_time=NOW,
    )

    await reconciler.async_close()
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_refresh_reconciles_selected_events_once(hass) -> None:
    """Account-level alarms are filtered and merged through the shared reconciler."""
    selected = EzvizAlarmEvent(
        alarm_id="alarm-selected",
        device_serial=SERIAL_1,
        channel_no=1,
        alarm_type="human_detection",
        occurred_at=NOW,
        image_url="https://example.invalid/alarm.jpg",
        source=EventSource.POLL,
    )
    ignored = EzvizAlarmEvent(
        alarm_id="alarm-ignored",
        device_serial=SERIAL_2,
        channel_no=1,
        alarm_type="motion",
        occurred_at=NOW,
        image_url=None,
        source=EventSource.POLL,
    )
    client = AsyncMock()
    client.async_list_alarms.return_value = (ignored, selected)
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=lambda: NOW, hold_seconds=60)
    persist = Mock()
    entry = _entry(
        options={
            CONF_SELECTED_DEVICES: [SERIAL_1],
            CONF_ALARM_INTERVAL: 15,
        }
    )
    coordinator = EzvizAlarmCoordinator(
        hass,
        entry,
        client,
        scheduler,
        reconciler,
        persist_callback=persist,
        now=lambda: NOW,
    )

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.update_interval == timedelta(seconds=15)
    assert coordinator.data.events == (selected,)
    assert coordinator.data.changed_count == 1
    state = reconciler.alarm_state_for(SERIAL_1, 1)
    assert state is not None
    assert state.motion is True
    assert state.person is True
    assert reconciler.alarm_state_for(SERIAL_2, 1) is None
    client.async_list_alarms.assert_awaited_once_with(
        start_time=NOW - timedelta(hours=24),
        end_time=NOW,
    )
    persist.assert_called_once_with()

    await coordinator.async_refresh()

    assert coordinator.data.changed_count == 0
    persist.assert_called_once_with()
    await reconciler.async_close()
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_refresh_ignores_devices_absent_from_metadata(hass) -> None:
    """Disabled shared devices must not leave hidden observed-event state."""
    event = EzvizAlarmEvent(
        alarm_id="alarm-shared-hidden",
        device_serial=SERIAL_1,
        channel_no=1,
        alarm_type="motion",
        occurred_at=NOW,
        image_url=None,
        source=EventSource.POLL,
    )
    client = AsyncMock()
    client.async_list_alarms.return_value = (event,)
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=lambda: NOW, hold_seconds=60)
    coordinator = EzvizAlarmCoordinator(
        hass,
        _entry(options={CONF_INCLUDE_SHARED_DEVICES: False}),
        client,
        scheduler,
        reconciler,
        included_device_serials=lambda: frozenset(),
        now=lambda: NOW,
    )

    try:
        await coordinator.async_config_entry_first_refresh()

        assert coordinator.data.events == ()
        assert reconciler.alarm_state_for(SERIAL_1, 1) is None
    finally:
        await reconciler.async_close()
        await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_poll_uses_incremental_overlap_after_empty_success(hass) -> None:
    """A successful empty poll advances the next bounded query window."""
    current_time = NOW

    def now() -> datetime:
        return current_time

    client = AsyncMock()
    client.async_list_alarms.return_value = ()
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=now, hold_seconds=60)
    coordinator = EzvizAlarmCoordinator(
        hass,
        _entry(),
        client,
        scheduler,
        reconciler,
        now=now,
    )

    await coordinator._async_update_data()
    current_time = NOW + timedelta(minutes=5)
    await coordinator._async_update_data()

    assert client.async_list_alarms.await_args_list == [
        call(
            start_time=NOW - timedelta(hours=24),
            end_time=NOW,
        ),
        call(
            start_time=NOW - timedelta(seconds=60),
            end_time=NOW + timedelta(minutes=5),
        ),
    ]
    await reconciler.async_close()
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_poll_failure_does_not_advance_checkpoint(hass) -> None:
    """A failed API request must leave the next recovery window unchanged."""
    current_time = NOW

    def now() -> datetime:
        return current_time

    client = AsyncMock()
    client.async_list_alarms.side_effect = [
        (),
        EzvizConnectionError("offline"),
        (),
    ]
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=now, hold_seconds=60)
    coordinator = EzvizAlarmCoordinator(
        hass,
        _entry(),
        client,
        scheduler,
        reconciler,
        now=now,
    )

    await coordinator._async_update_data()
    current_time = NOW + timedelta(minutes=5)
    with pytest.raises(UpdateFailed, match="EZVIZ alarm update failed"):
        await coordinator._async_update_data()
    current_time = NOW + timedelta(minutes=10)
    await coordinator._async_update_data()

    assert client.async_list_alarms.await_args_list[-1] == call(
        start_time=NOW - timedelta(seconds=60),
        end_time=NOW + timedelta(minutes=10),
    )
    await reconciler.async_close()
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_alarm_poll_clamps_future_checkpoint_after_clock_regression(hass) -> None:
    """A clock regression cannot produce an inverted or unbounded range."""
    current_time = NOW

    def now() -> datetime:
        return current_time

    client = AsyncMock()
    client.async_list_alarms.return_value = ()
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    registry = ObservedEntityRegistry("8ab35d94-a5ec-5c8a-a5bc-48e8e293722b")
    reconciler = EventReconciler(registry, now=now, hold_seconds=60)
    coordinator = EzvizAlarmCoordinator(
        hass,
        _entry(),
        client,
        scheduler,
        reconciler,
        now=now,
    )

    await coordinator._async_update_data()
    current_time = NOW - timedelta(hours=1)
    await coordinator._async_update_data()

    assert client.async_list_alarms.await_args_list[-1] == call(
        start_time=current_time,
        end_time=current_time,
    )
    await reconciler.async_close()
    await scheduler.async_close()


@pytest.mark.asyncio
async def test_authentication_errors_request_reauthentication(hass) -> None:
    """Rejected cloud credentials become Home Assistant auth failures."""
    client = AsyncMock()
    client.async_list_devices.side_effect = EzvizAuthError("rejected")
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizStateCoordinator(hass, _entry(), client, scheduler)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_config_entry_first_refresh()

    await scheduler.async_close()


@pytest.mark.asyncio
async def test_temporary_api_errors_become_update_failed(hass) -> None:
    """Transient cloud failures remain retryable coordinator failures."""
    client = AsyncMock()
    client.async_list_devices.side_effect = EzvizConnectionError("offline")
    scheduler = HubRequestScheduler(worker_count=1, max_queue_size=4)
    coordinator = EzvizStateCoordinator(hass, _entry(), client, scheduler)

    with pytest.raises(UpdateFailed, match="EZVIZ state update failed"):
        await coordinator._async_update_data()

    await scheduler.async_close()
