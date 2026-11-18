"""Tests for bounded EZVIZ image entities and scheduled captures."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers.typing import UNDEFINED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn import image as image_platform
from custom_components.ezviz_iot_cn.api.models import (
    AlarmSoundMode,
    CallingAction,
    EventSource,
    EzvizAlarmEvent,
    EzvizCallingEvent,
    EzvizCapabilities,
    EzvizCapture,
    EzvizChannel,
    EzvizDevice,
    channel_uid,
    device_uid,
    hub_uid,
)
from custom_components.ezviz_iot_cn.const import (
    CONF_CAPTURE_CHANNELS,
    CONF_CAPTURE_INTERVAL,
    CONF_ENABLE_ALARM_IMAGES,
    CONF_ENABLE_SCHEDULED_CAPTURES,
    DOMAIN,
)
from custom_components.ezviz_iot_cn.coordinator import (
    EzvizMetadataCoordinator,
    EzvizStateCoordinator,
    MetadataSnapshot,
    StateSnapshot,
)
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.runtime import (
    MAX_IMAGE_BYTES,
    EzvizImageCache,
    ImageCacheKey,
    ImageKind,
)
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
APP_KEY = "synthetic-app-key"
SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"
JPEG = b"\xff\xd8\xff\xe0synthetic-image"


def _device(serial: str) -> EzvizDevice:
    return EzvizDevice(
        serial=serial,
        name=f"Camera {serial[-1]}",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=AlarmSoundMode.SILENT,
        firmware_version="V1.0.0",
    )


def _channel(serial: str) -> EzvizChannel:
    return EzvizChannel(
        device_serial=serial,
        channel_no=1,
        name=f"Channel {serial[-1]}",
        online=True,
        encrypted=True,
    )


def _capabilities(serial: str, *, capture: bool) -> EzvizCapabilities:
    return EzvizCapabilities(
        device_serial=serial,
        supports_capture=capture,
        supports_defence=True,
        supports_ptz=False,
        ptz_top_bottom=False,
        ptz_left_right=False,
    )


def _entry(*, options: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic EZVIZ hub",
        data={"app_key": APP_KEY, "app_secret": "synthetic-secret"},
        options=options,
        unique_id=hub_uid(APP_KEY),
    )


def _install_runtime(
    hass: Any,
    entry: MockConfigEntry,
    metadata: MetadataSnapshot,
) -> tuple[EzvizMetadataCoordinator, EzvizStateCoordinator, EventReconciler]:
    client = AsyncMock()
    scheduler = Mock()
    metadata_coordinator = EzvizMetadataCoordinator(hass, entry, client, scheduler)
    state_coordinator = EzvizStateCoordinator(hass, entry, client, scheduler)
    metadata_coordinator.async_set_updated_data(metadata)
    state_coordinator.async_set_updated_data(StateSnapshot(metadata.devices))
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
            image_cache=EzvizImageCache(),
            capture_task=None,
        ),
    )
    return metadata_coordinator, state_coordinator, reconciler


def test_image_cache_separates_kinds_and_rejects_oversized_content() -> None:
    """Each channel keeps bounded capture, alarm, and calling buffers."""
    cache = EzvizImageCache()
    capture_key = ImageCacheKey(SERIAL_1, 1, ImageKind.CAPTURE)
    alarm_key = ImageCacheKey(SERIAL_1, 1, ImageKind.ALARM)
    calling_key = ImageCacheKey(SERIAL_1, 1, ImageKind.CALLING)

    cache.set(capture_key, JPEG, "image/jpeg", NOW)
    cache.set(alarm_key, b"alarm", "image/png", NOW)
    cache.set(calling_key, b"calling", "image/webp", NOW)

    assert cache.get(capture_key).content == JPEG
    assert cache.get(alarm_key).content == b"alarm"
    assert cache.get(capture_key).content_type == "image/jpeg"
    assert cache.get(calling_key).content == b"calling"
    assert cache.count == 3
    with pytest.raises(ValueError, match="8 MiB"):
        cache.set(capture_key, b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg", NOW)
    assert cache.get(capture_key).content == JPEG

    cache.clear()
    assert cache.count == 0


def test_image_cache_prune_releases_bytes_without_dropping_subscribers() -> None:
    """Removed channels release buffers while listeners survive rediscovery."""
    cache = EzvizImageCache()
    retained_key = ImageCacheKey(SERIAL_1, 1, ImageKind.ALARM)
    removed_key = ImageCacheKey(SERIAL_2, 1, ImageKind.ALARM)
    listener = Mock()
    cache.subscribe(removed_key, listener)
    cache.set(retained_key, JPEG, "image/jpeg", NOW)
    cache.set(removed_key, b"old", "image/png", NOW)
    listener.reset_mock()

    cache.prune({retained_key})

    assert cache.get(retained_key) is not None
    assert cache.get(removed_key) is None
    assert cache.count == 1
    cache.set(removed_key, b"new", "image/png", NOW)
    listener.assert_called_once()


@pytest.mark.asyncio
async def test_image_entities_follow_options_capabilities_and_hide_urls(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture support and options determine entities without exposing URLs."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: True,
            CONF_ENABLE_ALARM_IMAGES: True,
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1), _device(SERIAL_2)),
            channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
            capabilities=(
                _capabilities(SERIAL_1, capture=True),
                _capabilities(SERIAL_2, capture=False),
            ),
        ),
    )
    loop_started = asyncio.Event()
    release_loop = asyncio.Event()

    async def capture_loop(_entry: Any) -> None:
        loop_started.set()
        await release_loop.wait()

    monkeypatch.setattr(image_platform, "async_capture_loop", capture_loop)
    batches: list[list[Any]] = []
    await image_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    await loop_started.wait()

    entities = batches[0]
    assert [entity.entity_description.key for entity in entities].count(
        "latest_capture"
    ) == 1
    assert all(entity.entity_description.key != "latest_alarm" for entity in entities)
    expected_channel = channel_uid(device_uid(entry.unique_id, SERIAL_1), 1)
    assert any(
        entity.unique_id == f"{expected_channel}:latest_capture" for entity in entities
    )
    assert all(entity.image_url is UNDEFINED for entity in entities)
    assert all(entity.available is False for entity in entities)
    assert all(entity.translation_placeholders == {} for entity in entities)

    release_loop.set()
    await entry.runtime_data.capture_task
    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_disabled_image_options_create_no_entities_or_tasks(hass: Any) -> None:
    """Disabling both image features allocates neither entities nor a loop."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: False,
            CONF_ENABLE_ALARM_IMAGES: False,
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1),),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1, capture=True),),
        ),
    )
    batches: list[list[Any]] = []

    await image_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )

    assert batches == []
    assert entry.runtime_data.capture_task is None
    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_metadata_refresh_prunes_removed_channel_image_bytes(hass: Any) -> None:
    """Metadata removal releases stale image content before entry unload."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: False,
            CONF_ENABLE_ALARM_IMAGES: True,
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1), _device(SERIAL_2)),
            channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
            capabilities=(
                _capabilities(SERIAL_1, capture=True),
                _capabilities(SERIAL_2, capture=True),
            ),
        ),
    )
    entry.runtime_data.observed_registry.observe(
        SERIAL_1,
        1,
        (ObservedSemantic.ALARM_IMAGE,),
    )
    entry.runtime_data.observed_registry.observe(
        SERIAL_2,
        1,
        (ObservedSemantic.ALARM_IMAGE,),
    )
    await image_platform.async_setup_entry(hass, entry, lambda _entities: None)
    retained_key = ImageCacheKey(SERIAL_1, 1, ImageKind.ALARM)
    removed_key = ImageCacheKey(SERIAL_2, 1, ImageKind.ALARM)
    entry.runtime_data.image_cache.set(retained_key, JPEG, "image/jpeg", NOW)
    entry.runtime_data.image_cache.set(removed_key, b"old", "image/png", NOW)

    try:
        metadata_coordinator.async_set_updated_data(
            MetadataSnapshot(
                devices=(_device(SERIAL_1),),
                channels=(_channel(SERIAL_1),),
                capabilities=(_capabilities(SERIAL_1, capture=True),),
            )
        )
        await hass.async_block_till_done()

        assert entry.runtime_data.image_cache.get(retained_key) is not None
        assert entry.runtime_data.image_cache.get(removed_key) is None
        assert entry.runtime_data.image_cache.count == 1
    finally:
        await metadata_coordinator.async_shutdown()
        await state_coordinator.async_shutdown()
        await reconciler.async_close()


@pytest.mark.asyncio
async def test_alarm_image_downloads_on_demand_and_reuses_bounded_cache(
    hass: Any,
) -> None:
    """Alarm URLs stay internal and a matching cached event is not downloaded twice."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: False,
            CONF_ENABLE_ALARM_IMAGES: True,
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1),),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1, capture=True),),
        ),
    )
    batches: list[list[Any]] = []
    await image_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    runtime = entry.runtime_data
    assert batches == []

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "ALARM"
        return await operation()

    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    runtime.client.async_download_image.return_value = (JPEG, "image/jpeg")
    await reconciler.async_process_alarm(
        EzvizAlarmEvent(
            alarm_id="synthetic-alarm",
            device_serial=SERIAL_1,
            channel_no=1,
            alarm_type="motion",
            occurred_at=NOW,
            image_url="https://example.invalid/alarm.jpg?temporary=secret",
            source=EventSource.POLL,
        )
    )
    alarm_image = next(
        entity
        for batch in batches
        for entity in batch
        if entity.entity_description.key == "latest_alarm"
    )

    assert alarm_image.available is True

    assert await alarm_image.async_image() == JPEG
    assert await alarm_image.async_image() == JPEG
    runtime.client.async_download_image.assert_awaited_once_with(
        "https://example.invalid/alarm.jpg?temporary=secret"
    )
    assert alarm_image.content_type == "image/jpeg"
    assert alarm_image.image_last_updated == NOW
    assert alarm_image.image_url is UNDEFINED

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_calling_image_is_created_lazily_and_kept_separate(hass: Any) -> None:
    """A real doorbell cover creates one independent on-demand image entity."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: False,
            CONF_ENABLE_ALARM_IMAGES: True,
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1),),
            channels=(_channel(SERIAL_1),),
            capabilities=(_capabilities(SERIAL_1, capture=True),),
        ),
    )
    batches: list[list[Any]] = []
    await image_platform.async_setup_entry(
        hass,
        entry,
        lambda entities: batches.append(list(entities)),
    )
    assert batches == []

    await reconciler.async_process_call(
        EzvizCallingEvent(
            calling_id="calling-image",
            device_serial=SERIAL_1,
            channel_no=1,
            action=CallingAction.RING,
            occurred_at=NOW,
            image_url="https://example.invalid/calling.jpg?temporary=secret",
            source=EventSource.PUSH,
        )
    )

    calling_image = next(
        entity
        for batch in batches
        for entity in batch
        if entity.entity_description.key == "latest_calling"
    )
    runtime = entry.runtime_data

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "ALARM"
        return await operation()

    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)
    runtime.client.async_download_image.return_value = (JPEG, "image/jpeg")

    assert calling_image.available is True
    assert await calling_image.async_image() == JPEG
    runtime.client.async_download_image.assert_awaited_once_with(
        "https://example.invalid/calling.jpg?temporary=secret"
    )
    assert (
        runtime.image_cache.get(ImageCacheKey(SERIAL_1, 1, ImageKind.CALLING))
        is not None
    )
    assert runtime.image_cache.get(ImageCacheKey(SERIAL_1, 1, ImageKind.ALARM)) is None

    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restrict_channels", [False, True])
async def test_capture_cycle_is_sequential_and_staggered_across_interval(
    hass: Any,
    restrict_channels: bool,
) -> None:
    """Scheduled channels run one at a time with an even bounded spacing."""
    entry = _entry(
        options={
            CONF_ENABLE_SCHEDULED_CAPTURES: True,
            CONF_ENABLE_ALARM_IMAGES: False,
            CONF_CAPTURE_INTERVAL: 600,
            **({CONF_CAPTURE_CHANNELS: [f"{SERIAL_2}:1"]} if restrict_channels else {}),
        }
    )
    entry.add_to_hass(hass)
    metadata_coordinator, state_coordinator, reconciler = _install_runtime(
        hass,
        entry,
        MetadataSnapshot(
            devices=(_device(SERIAL_1), _device(SERIAL_2)),
            channels=(_channel(SERIAL_1), _channel(SERIAL_2)),
            capabilities=(
                _capabilities(SERIAL_1, capture=True),
                _capabilities(SERIAL_2, capture=True),
            ),
        ),
    )
    runtime = entry.runtime_data
    captures: list[str] = []
    delays: list[float] = []

    async def capture(serial: str, channel_no: int) -> EzvizCapture:
        captures.append(serial)
        return EzvizCapture(serial, channel_no, f"https://example.invalid/{serial}.jpg")

    async def download(url: str) -> tuple[bytes, str]:
        return url.encode(), "image/jpeg"

    async def submit(operation: Any, *, priority: Any) -> Any:
        assert priority.name == "SCHEDULED_CAPTURE"
        return await operation()

    async def sleep(delay: float) -> None:
        delays.append(delay)

    runtime.client.async_capture.side_effect = capture
    runtime.client.async_download_image.side_effect = download
    runtime.scheduler.async_submit = AsyncMock(side_effect=submit)

    await image_platform.async_capture_cycle(entry, sleep=sleep)

    assert captures == ([SERIAL_2] if restrict_channels else [SERIAL_1, SERIAL_2])
    assert delays == ([600.0] if restrict_channels else [300.0, 300.0])
    assert runtime.image_cache.count == len(captures)
    await metadata_coordinator.async_shutdown()
    await state_coordinator.async_shutdown()
    await reconciler.async_close()
