"""Tests for conservative EZVIZ Open Platform usage estimates."""

from __future__ import annotations

import pytest

from custom_components.ezviz_iot_cn.quota import estimate_daily_usage


def test_daily_usage_estimate_covers_enabled_polling_and_capture() -> None:
    """The estimate counts documented request families without hidden guesses."""
    estimate = estimate_daily_usage(
        include_shared_devices=True,
        enable_alarm_polling=True,
        alarm_interval=10,
        state_interval=60,
        metadata_interval=21600,
        selected_device_count=3,
        enable_scheduled_captures=True,
        capture_interval=600,
        capture_capable_channel_count=2,
    )

    assert estimate.alarm_calls == 8640
    assert estimate.state_calls == 2880
    assert estimate.metadata_calls == 32
    assert estimate.capture_calls == 288
    assert estimate.total_api_calls == 11840
    assert estimate.image_downloads == 288


def test_disabled_optional_features_contribute_no_calls() -> None:
    """Disabled alarm polling and capture are excluded from the estimate."""
    estimate = estimate_daily_usage(
        include_shared_devices=False,
        enable_alarm_polling=False,
        alarm_interval=10,
        state_interval=120,
        metadata_interval=43200,
        selected_device_count=1,
        enable_scheduled_captures=False,
        capture_interval=300,
        capture_capable_channel_count=4,
    )

    assert estimate.alarm_calls == 0
    assert estimate.state_calls == 720
    assert estimate.metadata_calls == 8
    assert estimate.capture_calls == 0
    assert estimate.total_api_calls == 728
    assert estimate.image_downloads == 0


def test_verified_light_candidates_add_two_metadata_checks_each() -> None:
    """An unverified protocol may require both public 301 and 305 reads."""
    estimate = estimate_daily_usage(
        include_shared_devices=False,
        enable_alarm_polling=False,
        alarm_interval=10,
        state_interval=60,
        metadata_interval=21600,
        selected_device_count=3,
        enable_scheduled_captures=False,
        capture_interval=600,
        capture_capable_channel_count=0,
        light_capable_device_count=2,
    )
    assert estimate.metadata_calls == 48


@pytest.mark.parametrize(
    "changes",
    [
        {"state_interval": 0},
        {"metadata_interval": -1},
        {"selected_device_count": -1},
        {"capture_capable_channel_count": -1},
        {"light_capable_device_count": -1},
    ],
)
def test_estimate_rejects_invalid_unbounded_inputs(changes: dict[str, int]) -> None:
    """The pure estimator never accepts values that could hide bad arithmetic."""
    inputs = {
        "include_shared_devices": True,
        "enable_alarm_polling": True,
        "alarm_interval": 10,
        "state_interval": 60,
        "metadata_interval": 21600,
        "selected_device_count": 1,
        "enable_scheduled_captures": False,
        "capture_interval": 600,
        "capture_capable_channel_count": 0,
    }
    inputs.update(changes)

    with pytest.raises(ValueError):
        estimate_daily_usage(**inputs)
