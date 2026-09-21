"""Tests for stable entity identity and read-only EZVIZ entities."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn import binary_sensor as binary_sensor_platform
from custom_components.ezviz_iot_cn import button as button_platform
from custom_components.ezviz_iot_cn import select as select_platform
from custom_components.ezviz_iot_cn import sensor as sensor_platform
from custom_components.ezviz_iot_cn import switch as switch_platform
from custom_components.ezviz_iot_cn.api.client import EzvizApiClient
from custom_components.ezviz_iot_cn.api.errors import EzvizConnectionError
from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    AlarmSoundMode,
    CallingAction,
    EventSource,
    EzvizAlarmEvent,
    EzvizCallingEvent,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    PtzDirection,
    channel_uid,
    device_uid,
    hub_uid,
)
from custom_components.ezviz_iot_cn.api.scheduler import HubRequestScheduler
from custom_components.ezviz_iot_cn.api.usage import ApiUsageTracker
from custom_components.ezviz_iot_cn.const import (
    CONF_ENABLE_ALARM_POLLING,
    CONF_ENABLE_NATIVE_WEBHOOK,
    DOMAIN,
)
from custom_components.ezviz_iot_cn.coordinator import (
    EzvizMetadataCoordinator,
    EzvizStateCoordinator,
    MetadataSnapshot,
    StateSnapshot,
)
from custom_components.ezviz_iot_cn.entity import (
    hub_device_info,
    physical_device_info,
)
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
APP_KEY = "synthetic-app-key"
SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"


def _device(
    serial: str,
    *,
    name: str | None = None,
    online: bool = True,
    defence: int | None = 1,
    alarm_sound_mode: AlarmSoundMode | None = AlarmSoundMode.SILENT,
) -> EzvizDevice:
    return EzvizDevice(
        serial=serial,
        name=name or f"Camera {serial[-1]}",
        model="CS-SYNTHETIC",
        online=online,
        defence=defence,
        alarm_sound_mode=alarm_sound_mode,
        firmware_version="V1.0.0",
    )


def _channel(
    serial: str,
    *,
    channel_no: int = 1,
    name: str | None = None,
) -> EzvizChannel:
    return EzvizChannel(
        device_serial=serial,
        channel_no=channel_no,
        name=name or f"Channel {serial[-1]}",
        online=True,
        encrypted=True,
    )


def _capabilities(
    serial: str,
    *,
    supports_defence: bool = True,
    supports_ptz: bool = True,
    ptz_top_bottom: bool = True,
    ptz_left_right: bool = True,
) -> EzvizCapabilities:
    return EzvizCapabilities(
        device_serial=serial,
        supports_capture=True,
        supports_defence=supports_defence,
        supports_ptz=supports_ptz,
        ptz_top_bottom=ptz_top_bottom,
        ptz_left_right=ptz_left_right,
        alarm_voice_level=1,
    )


def _entry(*, options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic EZVIZ hub",
        data={"app_key": APP_KEY, "app_secret": "synthetic-secret"},
        options=options or {},
        unique_id=hub_uid(APP_KEY),
    )


def _install_runtime(
    hass: Any,
    entry: MockConfigEntry,
    metadata: MetadataSnapshot,
    state: StateSnapshot,
) -> tuple[EzvizMetadataCoordinator, EzvizStateCoordinator, EventReconciler]:
    client = AsyncMock()
    scheduler = Mock()
    metadata_coordinator = EzvizMetadataCoordinator(hass, entry, client, scheduler)
    state_coordinator = EzvizStateCoordinator(hass, entry, client, scheduler)
    metadata_coordinator.async_set_updated_data(metadata)
    state_coordinator.async_set_updated_data(state)
    observed_registry = ObservedEntityRegistry(entry.unique_id)
    reconciler = EventReconciler(
        observed_registry,
        now=lambda: NOW,
        hold_seconds=300,
    )
    entry.runtime_data = cast(
        Any,
        SimpleNamespace(
            metadata_coordinator=metadata_coordinator,
            state_coordinator=state_coordinator,
            reconciler=reconciler,
            observed_registry=observed_registry,
            hub_device_id="synthetic-hub-registry-id",
            client=client,
            scheduler=scheduler,
            ptz_tasks={},
            ptz_locks={},
        ),
    )
    return metadata_coordinator, state_coordinator, reconciler


def test_hub_and_physical_device_info_use_stable_non_secret_ids() -> None:
    """Registry identity is hub-scoped, stable, and does not expose credentials."""
    entry = _entry()
    first = _device(SERIAL_1, name="Original name")
    renamed = _device(SERIAL_1, name="Renamed camera")
    expected_device_id = device_uid(entry.unique_id, SERIAL_1)

    hub_info = hub_device_info(entry)
    first_info = physical_device_info(
        entry,
        first,
        via_device_id="synthetic-hub-registry-id",
    )
    renamed_info = physical_device_info(
        entry,
        renamed,
        via_device_id="synthetic-hub-registry-id",
    )

    assert hub_info["identifiers"] == {(DOMAIN, entry.unique_id)}
    assert hub_info["name"] == "Synthetic EZVIZ hub"
    assert first_info["identifiers"] == {(DOMAIN, expected_device_id)}
    assert renamed_info["identifiers"] == {(DOMAIN, expected_device_id)}
    assert first_info["via_device_id"] == "synthetic-hub-registry-id"
    assert first_info["serial_number"] == SERIAL_1
    assert first_info["name"] == "Original name"
    assert renamed_info["name"] == "Renamed camera"
    assert APP_KEY not in repr(hub_info)
    assert "synthetic-secret" not in repr(first_info)


@pytest.mark.asyncio
async def test_binary_entities_are_capability_driven_and_added_dynamically(
    hass: Any,
) -> None:
    """Real alarm semantics add stable entities without guessed capabilities."""
    entry = _entry()
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1),),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(_device(SERIAL_1),)),
    )
    batches: list[list[Any]] = []

    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )

    assert len(batches) == 1
    initial = batches[0]
    assert [entity.entity_description.key for entity in initial] == ["online"]
    expected_device_id = device_uid(entry.unique_id, SERIAL_1)
    expected_channel_id = channel_uid(expected_device_id, 1)
    assert {entity.unique_id for entity in initial} == {f"{expected_device_id}:online"}
    online = next(
        entity for entity in initial if entity.entity_description.key == "online"
    )
    assert online.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert online.entity_category is EntityCategory.DIAGNOSTIC

    assert await reconciler.async_process_alarm(
        EzvizAlarmEvent(
            alarm_id="alarm-motion",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="motion",
            occurred_at=NOW,
            image_url=None,
            source=EventSource.PUSH,
        )
    )
    assert len(batches) == 2
    assert [entity.entity_description.key for entity in batches[1]] == ["motion"]
    motion = batches[1][0]
    assert motion.unique_id == f"{expected_channel_id}:motion"
    assert motion.device_class is BinarySensorDeviceClass.MOTION
    assert motion.translation_key == "motion"
    assert motion.translation_placeholders == {}

    assert await reconciler.async_process_alarm(
        EzvizAlarmEvent(
            alarm_id="alarm-person",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="human_detection",
            occurred_at=NOW.replace(second=1),
            image_url=None,
            source=EventSource.PUSH,
        )
    )
    assert len(batches) == 3
    assert [entity.entity_description.key for entity in batches[2]] == ["person"]
    person = batches[2][0]
    assert person.unique_id == f"{expected_channel_id}:person"
    assert person.device_class is BinarySensorDeviceClass.OCCUPANCY

    metadata_coordinator.async_set_updated_data(
        MetadataSnapshot(
            devices=(
                _device(SERIAL_1, name="Renamed camera"),
                _device(SERIAL_2),
            ),
            channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
            capabilities=(
                _capabilities(SERIAL_1),
                _capabilities(SERIAL_2),
            ),
        )
    )

    assert len(batches) == 4
    assert [entity.entity_description.key for entity in batches[3]] == ["online"]
    assert all(SERIAL_1 not in (entity.unique_id or "") for entity in batches[3])

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_multi_channel_entities_keep_channel_in_translated_name(
    hass: Any,
) -> None:
    """Only multi-channel devices need the channel name in entity labels."""
    entry = _entry()
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1),),
            channels=(
                _channel(SERIAL_1, channel_no=1, name="Front"),
                _channel(SERIAL_1, channel_no=2, name="Rear"),
            ),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(_device(SERIAL_1),)),
    )
    await reconciler.async_process_alarm(
        EzvizAlarmEvent(
            alarm_id="alarm-multi-channel",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="motion",
            occurred_at=NOW,
            image_url=None,
            source=EventSource.PUSH,
        )
    )
    batches: list[list[Any]] = []

    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )

    motion = next(
        entity for entity in batches[0] if entity.entity_description.key == "motion"
    )
    assert motion.translation_key == "motion_channel"
    assert motion.translation_placeholders == {"channel": "Front"}

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_read_only_entities_follow_state_and_reconciled_alarms(hass: Any) -> None:
    """A real alarm lazily creates read-only state with typed values."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    binary_batches: list[list[Any]] = []
    sensor_batches: list[list[Any]] = []
    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: binary_batches.append(list(entities)),
    )
    await sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: sensor_batches.append(list(entities)),
    )
    binary_entities = binary_batches[0]
    online = next(
        entity
        for entity in binary_entities
        if entity.entity_description.key == "online"
    )

    assert online.available is True
    assert online.is_on is True
    assert [entity.entity_description.key for entity in binary_entities] == ["online"]
    assert [entity.entity_description.key for entity in sensor_batches[0]] == [
        "local_api_requests",
        "local_api_rate_limits",
    ]

    event = EzvizAlarmEvent(
        alarm_id="synthetic-alarm-1",
        device_serial=SERIAL_1,
        channel_no=1,
        alarm_type="human_detection",
        occurred_at=NOW,
        image_url="https://example.invalid/alarm.jpg",
        source=EventSource.POLL,
    )
    assert await reconciler.async_process_alarm(event) is True
    motion = next(
        entity
        for entity in binary_batches[1]
        if entity.entity_description.key == "motion"
    )
    person = next(
        entity
        for entity in binary_batches[1]
        if entity.entity_description.key == "person"
    )
    alarm_type = next(
        entity
        for entity in sensor_batches[1]
        if entity.entity_description.key == "last_alarm_type"
    )
    alarm_time = next(
        entity
        for entity in sensor_batches[1]
        if entity.entity_description.key == "last_alarm_time"
    )
    assert motion.is_on is True
    assert person.is_on is True
    assert alarm_type.native_value == "human_detection"
    assert alarm_time.native_value == NOW
    assert alarm_type.available is True
    assert alarm_time.available is True

    state_coordinator.async_set_updated_data(StateSnapshot(devices=()))
    assert online.available is False
    assert motion.available is False
    assert alarm_type.available is False

    state_coordinator.async_set_updated_data(
        StateSnapshot(devices=(_device(SERIAL_1, online=False),))
    )
    assert online.available is True
    assert online.is_on is False
    assert motion.available is True

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_calling_entities_distinguish_status_from_real_ring(hass: Any) -> None:
    """Answer/hang-up expose call state without inventing a doorbell ring."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    binary_batches: list[list[Any]] = []
    sensor_batches: list[list[Any]] = []
    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: binary_batches.append(list(entities)),
    )
    await sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: sensor_batches.append(list(entities)),
    )

    assert await reconciler.async_process_call(
        EzvizCallingEvent(
            calling_id="calling-answer",
            device_serial=SERIAL_1,
            channel_no=1,
            action=CallingAction.ANSWER,
            occurred_at=NOW,
            image_url=None,
            source=EventSource.PUSH,
        )
    )

    assert len(binary_batches) == 1
    assert {entity.entity_description.key for entity in sensor_batches[1]} == {
        "last_calling_time",
        "calling_action",
    }
    calling_time = next(
        entity
        for entity in sensor_batches[1]
        if entity.entity_description.key == "last_calling_time"
    )
    calling_action = next(
        entity
        for entity in sensor_batches[1]
        if entity.entity_description.key == "calling_action"
    )
    assert calling_time.native_value == NOW
    assert calling_action.native_value == "answer"

    assert await reconciler.async_process_call(
        EzvizCallingEvent(
            calling_id="calling-ring",
            device_serial=SERIAL_1,
            channel_no=1,
            action=CallingAction.RING,
            occurred_at=NOW.replace(second=1),
            image_url=None,
            source=EventSource.PUSH,
        )
    )

    assert len(binary_batches) == 2
    assert [entity.entity_description.key for entity in binary_batches[1]] == [
        "doorbell_ring"
    ]
    assert binary_batches[1][0].is_on is True
    assert calling_time.native_value == NOW.replace(second=1)
    assert calling_action.native_value == "ring"

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_persisted_semantics_recreate_entities_without_live_values(
    hass: Any,
) -> None:
    """Restart evidence recreates entities but does not invent current events."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    entry.runtime_data.observed_registry.observe(
        SERIAL_1,
        1,
        {
            ObservedSemantic.MOTION,
            ObservedSemantic.CALLING,
            ObservedSemantic.DOORBELL_RING,
        },
    )
    binary_batches: list[list[Any]] = []
    sensor_batches: list[list[Any]] = []

    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: binary_batches.append(list(entities)),
    )
    await sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: sensor_batches.append(list(entities)),
    )

    binary = {entity.entity_description.key: entity for entity in binary_batches[0]}
    sensors = {entity.entity_description.key: entity for entity in sensor_batches[0]}
    assert set(binary) == {"online", "motion", "doorbell_ring"}
    assert binary["motion"].is_on is False
    assert binary["doorbell_ring"].is_on is False
    assert set(sensors) == {"last_calling_time", "calling_action"}
    assert all(entity.native_value is None for entity in sensors.values())
    assert all(entity.available is False for entity in sensors.values())

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_alarm_entities_require_an_enabled_event_transport(hass: Any) -> None:
    """No inert alarm entities are created when polling and webhook are off."""
    entry = _entry(
        options={
            CONF_ENABLE_ALARM_POLLING: False,
            CONF_ENABLE_NATIVE_WEBHOOK: False,
        }
    )
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    binary_batches: list[list[Any]] = []
    sensor_batches: list[list[Any]] = []
    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: binary_batches.append(list(entities)),
    )
    await sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: sensor_batches.append(list(entities)),
    )

    assert [entity.entity_description.key for entity in binary_batches[0]] == ["online"]
    assert [entity.entity_description.key for entity in sensor_batches[0]] == [
        "local_api_requests",
        "local_api_rate_limits",
    ]

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_defence_requires_a_real_current_state(hass: Any) -> None:
    """A capability alone does not make a stateless defence control usable."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1, defence=None)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await switch_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )

    assert len(batches[0]) == 1
    assert batches[0][0].is_on is None
    assert batches[0][0].available is False

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_alarm_entity_unsubscribes_from_reconciler_on_remove(hass: Any) -> None:
    """Alarm entities release reconciler and coordinator listeners on removal."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    entry.runtime_data.observed_registry.observe(
        SERIAL_1,
        1,
        {ObservedSemantic.MOTION},
    )
    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    motion = next(
        entity for entity in batches[0] if entity.entity_description.key == "motion"
    )
    write_state = Mock()
    motion.hass = hass
    motion.entity_id = "binary_sensor.synthetic_motion"
    motion.async_write_ha_state = write_state
    try:
        await motion.async_added_to_hass()

        first = EzvizAlarmEvent(
            alarm_id="synthetic-alarm-1",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="motion",
            occurred_at=NOW,
            image_url=None,
            source=EventSource.POLL,
        )
        assert await reconciler.async_process_alarm(first) is True
        write_state.assert_called_once_with()

        await motion.async_remove(force_remove=True)
        second = EzvizAlarmEvent(
            alarm_id="synthetic-alarm-2",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="motion",
            occurred_at=NOW.replace(second=1),
            image_url=None,
            source=EventSource.POLL,
        )
        assert await reconciler.async_process_alarm(second) is True
        write_state.assert_called_once_with()
    finally:
        await metadata_coordinator.async_shutdown()
        await state_coordinator.async_shutdown()
        await reconciler.async_close()


@pytest.mark.asyncio
async def test_control_entities_are_gated_by_normalized_capabilities(hass: Any) -> None:
    """Only verified capabilities create writable entities and PTZ directions."""
    entry = _entry()
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(
                _device(SERIAL_1),
                _device(SERIAL_2, alarm_sound_mode=None),
            ),
            channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
            capabilities=(
                _capabilities(SERIAL_1),
                _capabilities(
                    SERIAL_2,
                    supports_defence=False,
                    supports_ptz=True,
                    ptz_top_bottom=True,
                    ptz_left_right=False,
                ),
            ),
        ),
        StateSnapshot(
            devices=(
                _device(SERIAL_1),
                _device(SERIAL_2, alarm_sound_mode=None),
            )
        ),
    )
    switch_batches: list[list[Any]] = []
    select_batches: list[list[Any]] = []
    button_batches: list[list[Any]] = []

    await switch_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: switch_batches.append(list(entities)),
    )
    await select_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: select_batches.append(list(entities)),
    )
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: button_batches.append(list(entities)),
    )

    assert [entity.entity_description.key for entity in switch_batches[0]] == [
        "defence"
    ]
    assert [entity.entity_description.key for entity in select_batches[0]] == [
        "alarm_sound_mode"
    ]
    assert len(button_batches) == 2
    buttons = button_batches[0]
    device_1_buttons = [
        entity for entity in buttons if entity._device_serial == SERIAL_1
    ]
    device_2_buttons = [
        entity for entity in buttons if entity._device_serial == SERIAL_2
    ]
    assert all(entity.translation_placeholders == {} for entity in buttons)
    assert {entity.entity_description.key for entity in device_1_buttons} == {
        "ptz_up",
        "ptz_down",
        "ptz_left",
        "ptz_right",
        "refresh_device_state",
    }
    assert {entity.entity_description.key for entity in device_2_buttons} == {
        "ptz_up",
        "ptz_down",
        "refresh_device_state",
    }

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_entities_become_unavailable_when_channel_or_capability_disappears(
    hass: Any,
) -> None:
    """Registered entities never claim support after metadata removes it."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    binary_batches: list[list[Any]] = []
    switch_batches: list[list[Any]] = []
    button_batches: list[list[Any]] = []
    entry.runtime_data.observed_registry.observe(
        SERIAL_1,
        1,
        {ObservedSemantic.MOTION},
    )
    await binary_sensor_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: binary_batches.append(list(entities)),
    )
    await switch_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: switch_batches.append(list(entities)),
    )
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: button_batches.append(list(entities)),
    )
    motion = next(
        entity
        for entity in binary_batches[0]
        if entity.entity_description.key == "motion"
    )
    defence = switch_batches[0][0]
    up = next(
        entity
        for entity in button_batches[0]
        if entity.entity_description.key == "ptz_up"
    )
    assert motion.available is True
    assert defence.available is True
    assert up.available is True

    metadata_coordinator.async_set_updated_data(
        MetadataSnapshot(
            devices=(device,),
            channels=(),
            capabilities=(
                _capabilities(
                    SERIAL_1,
                    supports_defence=False,
                    supports_ptz=False,
                    ptz_top_bottom=False,
                    ptz_left_right=False,
                ),
            ),
        )
    )

    assert motion.available is False
    assert defence.available is False
    assert up.available is False

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_defence_and_alarm_sound_use_user_priority_and_refresh(hass: Any) -> None:
    """Each successful write refreshes the coordinator that owns its state."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(_device(SERIAL_1, alarm_sound_mode=None),)),
    )
    switch_batches: list[list[Any]] = []
    select_batches: list[list[Any]] = []
    await switch_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: switch_batches.append(list(entities)),
    )
    await select_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: select_batches.append(list(entities)),
    )
    defence = switch_batches[0][0]
    alarm_sound = select_batches[0][0]
    runtime = entry.runtime_data
    priorities: list[Any] = []

    async def submit(operation: Any, *, priority: Any) -> Any:
        priorities.append(priority)
        return await operation()

    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    state_coordinator.async_request_refresh = AsyncMock()
    metadata_coordinator.async_request_refresh = AsyncMock()

    assert defence.is_on is True
    await defence.async_turn_off()
    runtime.client.async_set_defence.assert_awaited_once_with(
        SERIAL_1,
        enabled=False,
    )
    assert defence.is_on is True
    state_coordinator.async_request_refresh.assert_awaited_once_with()

    assert alarm_sound.options == ["short_alert", "long_alert", "silent"]
    assert alarm_sound.current_option == "silent"
    assert alarm_sound.available is True
    await alarm_sound.async_select_option("short_alert")
    runtime.client.async_set_alarm_sound.assert_awaited_once_with(
        SERIAL_1,
        AlarmSoundMode.SHORT_ALERT,
    )
    assert alarm_sound.current_option == "silent"
    metadata_coordinator.async_request_refresh.assert_awaited_once_with()
    state_coordinator.async_request_refresh.assert_awaited_once_with()
    assert all(priority.name == "USER_CONTROL" for priority in priorities)

    runtime.client.async_set_defence.side_effect = EzvizConnectionError("offline")
    with pytest.raises(HomeAssistantError, match="defence"):
        await defence.async_turn_on()
    assert defence.is_on is True
    state_coordinator.async_request_refresh.assert_awaited_once_with()

    with pytest.raises(ValueError, match="Unsupported alarm sound option"):
        await alarm_sound.async_select_option("unknown")

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_alarm_flash_needs_verified_state_and_uses_saved_protocol(
    hass: Any,
) -> None:
    """Proven light controls do not conflate sound or arming and never guess state."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = replace(
        _device(SERIAL_1, alarm_sound_mode=AlarmSoundMode.SILENT),
        alarm_light_enabled=True,
        alarm_light_protocol=AlarmLightProtocol.PIR,
    )
    capabilities = replace(
        _capabilities(SERIAL_1), alarm_voice_level=0, active_defense_level=2
    )
    metadata, state, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot((device,), (_channel(SERIAL_1),), (capabilities,)),
        StateSnapshot((device,)),
    )
    switches = switch_platform._entities_for_snapshot(entry, metadata.data)
    flash = next(
        item for item in switches if item.entity_description.key == "alarm_flash"
    )
    assert tuple(select_platform._entities_for_snapshot(entry, metadata.data)) == ()
    assert flash.unique_id == f"{device_uid(entry.unique_id, SERIAL_1)}:alarm_flash"
    assert flash.is_on is True
    assert flash.available is True

    async def submit(operation, *, priority):
        assert priority.name == "USER_CONTROL"
        return await operation()

    entry.runtime_data.scheduler.async_submit = AsyncMock(side_effect=submit)
    metadata.async_request_refresh = AsyncMock()
    await flash.async_turn_off()
    entry.runtime_data.client.async_set_alarm_light.assert_awaited_once_with(
        SERIAL_1, protocol=AlarmLightProtocol.PIR, enabled=False
    )
    metadata.async_request_refresh.assert_awaited_once_with()
    assert flash.is_on is True

    state.async_set_updated_data(StateSnapshot((replace(device, online=False),)))
    assert flash.available is False
    state.async_set_updated_data(StateSnapshot((device,)))
    assert flash.available is True

    metadata.async_set_updated_data(
        MetadataSnapshot(
            (replace(device, online=False, alarm_light_enabled=None),),
            (_channel(SERIAL_1),),
            (capabilities,),
        )
    )
    assert flash.available is False
    assert flash.is_on is None
    with pytest.raises(HomeAssistantError, match="not verified"):
        await flash.async_turn_on()
    entry.runtime_data.client.async_set_alarm_light.assert_awaited_once()

    unsupported = MetadataSnapshot(
        (device,),
        (_channel(SERIAL_1),),
        (replace(capabilities, active_defense_level=1),),
    )
    assert all(
        item.entity_description.key != "alarm_flash"
        for item in switch_platform._entities_for_snapshot(entry, unsupported)
    )

    await metadata.async_shutdown()
    await state.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_hub_usage_sensors_and_refresh_button_never_request_quota(
    hass: Any,
) -> None:
    """Hub counters label local attempts and a manual display refresh is local."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata, state, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot((device,), (_channel(SERIAL_1),), (_capabilities(SERIAL_1),)),
        StateSnapshot((device,)),
    )
    tracker = ApiUsageTracker(now=lambda: NOW)
    entry.runtime_data.client = EzvizApiClient(
        Mock(), app_key=APP_KEY, app_secret="synthetic-secret", usage_tracker=tracker
    )
    request_sensor = sensor_platform.EzvizLocalUsageSensor(
        entry, sensor_platform.API_ATTEMPTS_DESCRIPTION
    )
    rate_sensor = sensor_platform.EzvizLocalUsageSensor(
        entry, sensor_platform.API_RATE_LIMITS_DESCRIPTION
    )
    button = button_platform.EzvizRefreshUsageButton(entry)
    notifications: list[bool] = []
    unsubscribe = tracker.subscribe(notifications.append)
    assert request_sensor.native_value == 0
    tracker.record_request()
    tracker.record_rate_limit()
    assert request_sensor.native_value == 1
    assert rate_sensor.native_value == 1
    await button.async_press()
    assert notifications == [False, False, True]
    assert tracker.snapshot.request_attempts == 1
    unsubscribe()
    await metadata.async_shutdown()
    await state.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_refresh_device_button_stays_available_after_state_poll_failure(
    hass: Any,
) -> None:
    """Manual refresh remains usable for recovery, but excluded devices do not."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata, state, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot((device,), (_channel(SERIAL_1),), (_capabilities(SERIAL_1),)),
        StateSnapshot((device,)),
    )
    button = button_platform.EzvizRefreshDeviceButton(entry, device)
    state.async_set_updated_data(StateSnapshot(()))
    assert button.available is True
    entry.runtime_data.async_refresh_device_state = AsyncMock()
    await button.async_press()
    entry.runtime_data.async_refresh_device_state.assert_awaited_once_with()
    metadata.async_set_updated_data(MetadataSnapshot((), (), ()))
    assert button.available is False
    await metadata.async_shutdown()
    await state.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_ptz_direction_always_stops_after_success_and_start_error(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every direction action issues its matching stop in a finally block."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    up = next(
        entity for entity in batches[0] if entity.entity_description.key == "ptz_up"
    )
    runtime = entry.runtime_data
    priorities: list[Any] = []

    async def submit(operation: Any, *, priority: Any) -> Any:
        priorities.append(priority)
        return await operation()

    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    monkeypatch.setattr(button_platform, "PTZ_MOVE_SECONDS", 0)

    await up.async_press()
    runtime.client.async_ptz_start.assert_awaited_once_with(
        SERIAL_1,
        channel_no=1,
        direction=PtzDirection.UP,
        speed=1,
    )
    runtime.client.async_ptz_stop.assert_awaited_once_with(
        SERIAL_1,
        channel_no=1,
        direction=PtzDirection.UP,
    )
    assert all(priority.name == "USER_CONTROL" for priority in priorities)

    runtime.client.async_ptz_start.reset_mock()
    runtime.client.async_ptz_stop.reset_mock()
    runtime.client.async_ptz_start.side_effect = EzvizConnectionError("start failed")
    with pytest.raises(HomeAssistantError, match="PTZ"):
        await up.async_press()
    runtime.client.async_ptz_stop.assert_awaited_once_with(
        SERIAL_1,
        channel_no=1,
        direction=PtzDirection.UP,
    )

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_ptz_async_task_names_use_only_non_reversible_identity(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal task labels must not expose the physical device serial."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    up = next(
        entity for entity in batches[0] if entity.entity_description.key == "ptz_up"
    )
    runtime = entry.runtime_data

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "USER_CONTROL"
        return await operation()

    created_names: list[str | None] = []
    real_create_task = asyncio.create_task

    def create_task(coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        created_names.append(name)
        return real_create_task(coro, name=name)

    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    monkeypatch.setattr(button_platform.asyncio, "create_task", create_task)
    monkeypatch.setattr(button_platform, "PTZ_MOVE_SECONDS", 0)

    await up.async_press()

    assert len(created_names) == 2
    assert all(name is not None for name in created_names)
    assert all(SERIAL_1 not in name for name in created_names if name is not None)
    assert all(up._device_id in name for name in created_names if name is not None)

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_ptz_cancellation_uses_channel_scoped_runtime_key(
    hass: Any,
) -> None:
    """PTZ ownership is isolated by physical device and channel."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    up = next(
        entity for entity in batches[0] if entity.entity_description.key == "ptz_up"
    )
    assert all(entity.entity_description.key != "ptz_stop" for entity in batches[0])
    runtime = entry.runtime_data
    started = asyncio.Event()

    async def start(*args: Any, **kwargs: Any) -> None:
        started.set()

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "USER_CONTROL"
        return await operation()

    runtime.client.async_ptz_start.side_effect = start
    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)

    press_task = hass.async_create_task(up.async_press())
    await started.wait()
    key = (SERIAL_1, 1)
    assert key in runtime.ptz_tasks
    press_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await press_task
    runtime.client.async_ptz_stop.assert_awaited_once_with(
        SERIAL_1,
        channel_no=1,
        direction=PtzDirection.UP,
    )
    assert key not in runtime.ptz_tasks

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_ptz_channels_on_one_device_run_independently(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One channel movement never cancels another channel on the same recorder."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(
                _channel(SERIAL_1, channel_no=1, name="Channel 1"),
                _channel(SERIAL_1, channel_no=2, name="Channel 2"),
            ),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    up_buttons = sorted(
        (entity for entity in batches[0] if entity.entity_description.key == "ptz_up"),
        key=lambda entity: entity._channel_no,
    )
    runtime = entry.runtime_data
    started = {1: asyncio.Event(), 2: asyncio.Event()}

    async def start(
        _serial: str,
        *,
        channel_no: int,
        direction: PtzDirection,
        speed: int,
    ) -> None:
        assert direction is PtzDirection.UP
        assert speed == 1
        started[channel_no].set()

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "USER_CONTROL"
        return await operation()

    runtime.client.async_ptz_start.side_effect = start
    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    monkeypatch.setattr(button_platform, "PTZ_MOVE_SECONDS", 60)
    tasks = [hass.async_create_task(button.async_press()) for button in up_buttons]
    try:
        await asyncio.wait_for(started[1].wait(), timeout=1)
        await asyncio.wait_for(started[2].wait(), timeout=1)
        assert set(runtime.ptz_tasks) == {(SERIAL_1, 1), (SERIAL_1, 2)}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await metadata_coordinator.async_shutdown()
        await state_coordinator.async_shutdown()
        await reconciler.async_close()

    assert runtime.client.async_ptz_stop.await_count == 2


@pytest.mark.asyncio
async def test_ptz_cancellation_waits_for_inflight_start_before_stop(hass: Any) -> None:
    """A second scheduler worker cannot run stop ahead of an in-flight start."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = _device(SERIAL_1)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(device,),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1),),
        ),
        StateSnapshot(devices=(device,)),
    )
    batches: list[list[Any]] = []
    await button_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    up = next(
        entity for entity in batches[0] if entity.entity_description.key == "ptz_up"
    )
    runtime = entry.runtime_data
    scheduler = HubRequestScheduler(worker_count=2, max_queue_size=8)
    runtime.scheduler = scheduler
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    stop_called = asyncio.Event()

    async def start(*args: Any, **kwargs: Any) -> None:
        start_entered.set()
        await release_start.wait()

    async def stop(*args: Any, **kwargs: Any) -> None:
        stop_called.set()

    runtime.client.async_ptz_start.side_effect = start
    runtime.client.async_ptz_stop.side_effect = stop
    press_task = hass.async_create_task(up.async_press())
    try:
        await start_entered.wait()
        press_task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stop_called.is_set() is False

        release_start.set()
        with pytest.raises(asyncio.CancelledError):
            await press_task
        assert stop_called.is_set() is True
    finally:
        release_start.set()
        await asyncio.gather(press_task, return_exceptions=True)
        await scheduler.async_close()
        await metadata_coordinator.async_shutdown()
        await state_coordinator.async_shutdown()
        await reconciler.async_close()
