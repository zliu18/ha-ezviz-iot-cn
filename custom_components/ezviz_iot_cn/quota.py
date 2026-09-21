"""Conservative daily EZVIZ Open Platform usage estimates."""

from __future__ import annotations

from dataclasses import dataclass

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True, slots=True)
class DailyUsageEstimate:
    """Minimum documented API calls and direct image downloads per day."""

    alarm_calls: int
    state_calls: int
    metadata_calls: int
    capture_calls: int
    total_api_calls: int
    image_downloads: int


def estimate_daily_usage(
    *,
    include_shared_devices: bool,
    enable_alarm_polling: bool,
    alarm_interval: int,
    state_interval: int,
    metadata_interval: int,
    selected_device_count: int,
    enable_scheduled_captures: bool,
    capture_interval: int,
    capture_capable_channel_count: int,
    light_capable_device_count: int = 0,
) -> DailyUsageEstimate:
    """Estimate baseline usage without guessing pagination, retries, or quota."""
    intervals = (alarm_interval, state_interval, metadata_interval, capture_interval)
    if any(interval <= 0 for interval in intervals):
        raise ValueError("Polling and capture intervals must be positive")
    if (
        min(
            selected_device_count,
            capture_capable_channel_count,
            light_capable_device_count,
        )
        < 0
    ):
        raise ValueError("Device and channel counts must not be negative")

    alarm_calls = _runs_per_day(alarm_interval) if enable_alarm_polling else 0
    state_calls = _runs_per_day(state_interval) * (2 if include_shared_devices else 1)
    metadata_calls = _runs_per_day(metadata_interval) * (
        2 + (2 * selected_device_count) + (2 * light_capable_device_count)
    )
    capture_calls = (
        _runs_per_day(capture_interval) * capture_capable_channel_count
        if enable_scheduled_captures
        else 0
    )
    return DailyUsageEstimate(
        alarm_calls=alarm_calls,
        state_calls=state_calls,
        metadata_calls=metadata_calls,
        capture_calls=capture_calls,
        total_api_calls=alarm_calls + state_calls + metadata_calls + capture_calls,
        image_downloads=capture_calls,
    )


def _runs_per_day(interval: int) -> int:
    return (_SECONDS_PER_DAY + interval - 1) // interval
