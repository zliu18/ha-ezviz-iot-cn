"""Privacy-preserving diagnostics for EZVIZ IoT CN."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, cast

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_INCLUDE_SHARED_DEVICES,
    CONF_WEBHOOK_ID,
)
from .coordinator import AlarmSnapshot, MetadataSnapshot, StateSnapshot
from .filtering import configured_device_serials, device_filter_mode
from .runtime import EzvizConfigEntry
from .x5s_time import CONF_X5S_HOST, CONF_X5S_PASSWORD, CONF_X5S_USERNAME

_REDACT_KEYS = {
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_X5S_HOST,
    CONF_X5S_PASSWORD,
    CONF_X5S_USERNAME,
    CONF_WEBHOOK_ID,
    "access_token",
    "alarm_id",
    "device_serial",
    "image_url",
    "raw_payload",
    "token",
    "webhook_path",
}


def _interval_seconds(value: timedelta | None) -> int | None:
    """Return a serializable coordinator interval."""
    if value is None:
        return None
    return int(value.total_seconds())


def _coordinator_health(coordinator: Any) -> dict[str, Any]:
    """Return health fields that contain no request or device data."""
    return {
        "last_update_success": bool(coordinator.last_update_success),
        "update_interval_seconds": _interval_seconds(coordinator.update_interval),
        "has_data": coordinator.data is not None,
    }


def _metadata_summary(data: MetadataSnapshot | None) -> dict[str, Any]:
    """Aggregate metadata without exposing model fields."""
    if data is None:
        return {
            "device_count": 0,
            "channel_count": 0,
            "capability_count": 0,
            "capabilities": {
                "capture": 0,
                "defence": 0,
                "ptz": 0,
            },
        }
    capabilities = data.capabilities
    return {
        "device_count": len(data.devices),
        "channel_count": len(data.channels),
        "capability_count": len(capabilities),
        "capabilities": {
            "capture": sum(item.supports_capture for item in capabilities),
            "defence": sum(item.supports_defence for item in capabilities),
            "ptz": sum(item.supports_ptz for item in capabilities),
        },
    }


def _state_summary(data: StateSnapshot | None) -> dict[str, int]:
    """Aggregate device connectivity without identifiers."""
    devices = () if data is None else data.devices
    online_count = sum(device.online for device in devices)
    return {
        "device_count": len(devices),
        "online_count": online_count,
        "offline_count": len(devices) - online_count,
    }


def _alarm_summary(
    data: AlarmSnapshot | None,
    *,
    recent_count: int,
    active_reset_count: int,
) -> dict[str, int]:
    """Aggregate alarm processing without event contents."""
    return {
        "last_poll_event_count": 0 if data is None else len(data.events),
        "last_poll_changed_count": 0 if data is None else data.changed_count,
        "recent_deduplication_count": recent_count,
        "active_reset_count": active_reset_count,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
) -> Mapping[str, Any]:
    """Return bounded diagnostics without credentials or device identifiers."""
    del hass
    runtime = entry.runtime_data
    scheduler = runtime.scheduler.stats
    queue = runtime.webhook_queue
    webhook_task = runtime.webhook_task
    webhook_stats = runtime.webhook_stats
    selected = configured_device_serials(entry.options)
    metadata = runtime.metadata_coordinator.data
    data: dict[str, Any] = {
        "configuration": {
            "include_shared_devices": bool(
                entry.options.get(CONF_INCLUDE_SHARED_DEVICES, True)
            ),
            "device_filter_mode": device_filter_mode(entry.options),
            "configured_filter_device_count": len(selected),
            "included_device_count": 0 if metadata is None else len(metadata.devices),
            "native_webhook_enabled": bool(
                entry.options.get(CONF_ENABLE_NATIVE_WEBHOOK, False)
            ),
        },
        "coordinators": {
            "metadata": _coordinator_health(runtime.metadata_coordinator),
            "state": _coordinator_health(runtime.state_coordinator),
            "alarms": _coordinator_health(runtime.alarm_coordinator),
        },
        "metadata": _metadata_summary(metadata),
        "state": _state_summary(runtime.state_coordinator.data),
        "scheduler": {
            "queue_size": scheduler.queue_size,
            "queue_high_watermark": scheduler.queue_high_watermark,
            "running_count": scheduler.running_count,
            "running_high_watermark": scheduler.high_watermark,
            "worker_count": scheduler.worker_count,
            "singleflight_count": scheduler.singleflight_count,
            "closed": scheduler.closed,
        },
        "webhook": {
            "enabled": bool(entry.options.get(CONF_ENABLE_NATIVE_WEBHOOK, False)),
            "registered": runtime.webhook_id is not None,
            "worker_running": webhook_task is not None and not webhook_task.done(),
            "queue_size": 0 if queue is None else queue.qsize(),
            "queue_max_size": 0 if queue is None else queue.maxsize,
            "valid_envelopes": webhook_stats.valid_envelopes,
            "unsupported_types": webhook_stats.unsupported_types,
            "unknown_targets": webhook_stats.unknown_targets,
            "invalid_messages": webhook_stats.invalid_messages,
            "worker_errors": webhook_stats.worker_errors,
            "queue_drops": webhook_stats.queue_drops,
            "processed_alarms": webhook_stats.processed_alarms,
            "processed_calls": webhook_stats.processed_calls,
        },
        "events": {
            "observed_semantic_count": runtime.observed_registry.observed_count,
        },
        "images": {"cached_slot_count": runtime.image_cache.count},
        "alarm_light": {
            "probe_reasons": dict(
                Counter(() if metadata is None else metadata.alarm_light_probe_reasons)
            ),
            "verified_device_count": sum(
                device.alarm_light_enabled is not None
                for device in (() if metadata is None else metadata.devices)
            ),
        },
        "api_usage_local": {
            "request_attempts": runtime.client.usage_snapshot.request_attempts,
            "rate_limit_events": runtime.client.usage_snapshot.rate_limit_events,
        },
        "alarms": _alarm_summary(
            runtime.alarm_coordinator.data,
            recent_count=runtime.reconciler.recent_count,
            active_reset_count=runtime.reconciler.active_reset_count,
        ),
        "ptz": {
            "active_movement_count": sum(
                not task.done() for task in runtime.ptz_tasks.values()
            )
        },
    }
    return cast(dict[str, Any], async_redact_data(data, _REDACT_KEYS))
