"""Tests for bounded, privacy-preserving diagnostics."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn.api.models import (
    AccessToken,
    AlarmSoundMode,
    EventSource,
    EzvizAlarmEvent,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
)
from custom_components.ezviz_iot_cn.api.scheduler import SchedulerStats
from custom_components.ezviz_iot_cn.api.usage import ApiUsageSnapshot
from custom_components.ezviz_iot_cn.const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_DEVICE_FILTER_MODE,
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_SELECTED_DEVICES,
    CONF_STATE_INTERVAL,
    CONF_WEBHOOK_ID,
    DOMAIN,
    FILTER_MODE_INCLUDE,
)
from custom_components.ezviz_iot_cn.coordinator import (
    AlarmSnapshot,
    MetadataSnapshot,
    StateSnapshot,
)
from custom_components.ezviz_iot_cn.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ezviz_iot_cn.runtime import (
    EzvizImageCache,
    ImageCacheKey,
    ImageKind,
    WebhookStats,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_diagnostics_expose_only_bounded_aggregate_health(hass: Any) -> None:
    """Diagnostics omit every credential, identifier, payload, URL, and byte body."""
    app_key = "SENTINEL-APP-KEY"
    app_secret = "SENTINEL-APP-SECRET"
    access_token = "SENTINEL-ACCESS-TOKEN"
    serial = "SENTINEL-DEVICE-SERIAL"
    webhook_id = "ezviz_iot_cn_SENTINEL-WEBHOOK-ID"
    image_url = "https://SENTINEL-IMAGE-URL.invalid/alarm.jpg"
    image_bytes = b"SENTINEL-IMAGE-BYTES"
    raw_payload = "SENTINEL-RAW-PAYLOAD"

    device = EzvizDevice(
        serial=serial,
        name="SENTINEL-DEVICE-NAME",
        model="SENTINEL-DEVICE-MODEL",
        online=True,
        defence=1,
        alarm_sound_mode=AlarmSoundMode.SILENT,
        firmware_version="SENTINEL-FIRMWARE",
    )
    channel = EzvizChannel(
        device_serial=serial,
        channel_no=7,
        name="SENTINEL-CHANNEL-NAME",
        online=True,
        encrypted=True,
    )
    capabilities = EzvizCapabilities(
        device_serial=serial,
        supports_capture=True,
        supports_defence=True,
        supports_ptz=True,
        ptz_top_bottom=True,
        ptz_left_right=False,
    )
    alarm = EzvizAlarmEvent(
        alarm_id=raw_payload,
        device_serial=serial,
        channel_no=7,
        alarm_type=raw_payload,
        occurred_at=NOW,
        image_url=image_url,
        source=EventSource.PUSH,
    )
    webhook_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=128)
    webhook_queue.put_nowait(object())
    image_cache = EzvizImageCache()
    image_cache.set(
        ImageCacheKey(serial, 7, ImageKind.ALARM),
        image_bytes,
        "image/jpeg",
        NOW,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SENTINEL-HUB-TITLE",
        data={
            CONF_APP_KEY: app_key,
            CONF_APP_SECRET: app_secret,
            CONF_WEBHOOK_ID: webhook_id,
        },
        options={
            CONF_DEVICE_FILTER_MODE: FILTER_MODE_INCLUDE,
            CONF_SELECTED_DEVICES: [serial],
            CONF_STATE_INTERVAL: 60,
            CONF_ENABLE_NATIVE_WEBHOOK: True,
        },
        unique_id="SENTINEL-HUB-UNIQUE-ID",
    )
    entry.add_to_hass(hass)
    runtime = SimpleNamespace(
        client=SimpleNamespace(
            _app_key=app_key,
            _app_secret=app_secret,
            _token=AccessToken(access_token, NOW + timedelta(hours=1)),
            usage_snapshot=ApiUsageSnapshot(8, 1, NOW, NOW, NOW),
        ),
        scheduler=SimpleNamespace(
            stats=SchedulerStats(
                queue_size=2,
                queue_high_watermark=5,
                running_count=1,
                high_watermark=2,
                worker_count=2,
                singleflight_count=1,
                closed=False,
            )
        ),
        metadata_coordinator=SimpleNamespace(
            last_update_success=True,
            update_interval=timedelta(hours=6),
            data=MetadataSnapshot(
                (device,),
                (channel,),
                (capabilities,),
                ("light_capable_but_public_status_unsupported",),
            ),
        ),
        state_coordinator=SimpleNamespace(
            last_update_success=True,
            update_interval=timedelta(seconds=60),
            data=StateSnapshot((device,)),
        ),
        alarm_coordinator=SimpleNamespace(
            last_update_success=True,
            update_interval=timedelta(seconds=10),
            data=AlarmSnapshot((alarm,), 1),
        ),
        reconciler=SimpleNamespace(recent_count=3, active_reset_count=1),
        observed_registry=SimpleNamespace(observed_count=7),
        image_cache=image_cache,
        webhook_id=webhook_id,
        webhook_queue=webhook_queue,
        webhook_task=SimpleNamespace(done=lambda: False),
        webhook_stats=WebhookStats(
            valid_envelopes=13,
            unsupported_types=2,
            unknown_targets=3,
            invalid_messages=1,
            worker_errors=6,
            queue_drops=4,
            processed_alarms=8,
            processed_calls=5,
        ),
        ptz_tasks={(serial, 7): SimpleNamespace(done=lambda: False)},
    )
    entry.runtime_data = cast(Any, runtime)

    result = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result, sort_keys=True)

    for sensitive in (
        app_key,
        app_secret,
        access_token,
        serial,
        webhook_id,
        image_url,
        image_bytes.decode(),
        raw_payload,
        "SENTINEL-HUB-TITLE",
        "SENTINEL-HUB-UNIQUE-ID",
        "SENTINEL-DEVICE-NAME",
        "SENTINEL-DEVICE-MODEL",
        "SENTINEL-CHANNEL-NAME",
        "SENTINEL-FIRMWARE",
    ):
        assert sensitive not in serialized

    assert result["metadata"] == {
        "device_count": 1,
        "channel_count": 1,
        "capability_count": 1,
        "capabilities": {
            "capture": 1,
            "defence": 1,
            "ptz": 1,
        },
    }
    assert result["configuration"] == {
        "include_shared_devices": True,
        "device_filter_mode": FILTER_MODE_INCLUDE,
        "configured_filter_device_count": 1,
        "included_device_count": 1,
        "native_webhook_enabled": True,
    }
    assert result["state"] == {
        "device_count": 1,
        "online_count": 1,
        "offline_count": 0,
    }
    assert result["scheduler"]["queue_high_watermark"] == 5
    assert result["webhook"] == {
        "enabled": True,
        "registered": True,
        "worker_running": True,
        "queue_size": 1,
        "queue_max_size": 128,
        "valid_envelopes": 13,
        "unsupported_types": 2,
        "unknown_targets": 3,
        "invalid_messages": 1,
        "worker_errors": 6,
        "queue_drops": 4,
        "processed_alarms": 8,
        "processed_calls": 5,
    }
    assert result["events"] == {"observed_semantic_count": 7}
    assert result["images"] == {"cached_slot_count": 1}
    assert result["alarm_light"] == {
        "probe_reasons": {"light_capable_but_public_status_unsupported": 1},
        "verified_device_count": 0,
    }
    assert result["api_usage_local"] == {
        "request_attempts": 8,
        "rate_limit_events": 1,
    }
    assert result["alarms"] == {
        "last_poll_event_count": 1,
        "last_poll_changed_count": 1,
        "recent_deduplication_count": 3,
        "active_reset_count": 1,
    }
    assert result["ptz"] == {"active_movement_count": 1}
