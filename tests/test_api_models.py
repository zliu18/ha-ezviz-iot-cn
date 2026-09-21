"""Tests for transport-independent EZVIZ API models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from custom_components.ezviz_iot_cn.api.models import (
    AccessToken,
    AlarmSoundMode,
    CallingAction,
    EventSource,
    EzvizAlarmEvent,
    EzvizCallingEvent,
    EzvizDevice,
    PtzDirection,
    channel_uid,
    device_uid,
    hub_uid,
)


def test_identity_hierarchy_is_deterministic_and_non_reversible() -> None:
    """Stable IDs must not expose credentials or device serials."""
    app_key = "synthetic-app-key-001"
    serial = "SYNTHETIC-SERIAL-001"

    first_hub = hub_uid(app_key)
    second_hub = hub_uid(app_key)
    first_device = device_uid(first_hub, serial)
    first_channel = channel_uid(first_device, 1)

    assert first_hub == second_hub
    assert first_hub != hub_uid("synthetic-app-key-002")
    assert first_device != device_uid(first_hub, "SYNTHETIC-SERIAL-002")
    assert first_channel != channel_uid(first_device, 2)
    assert UUID(first_hub).version == 5
    assert UUID(first_device).version == 5
    assert UUID(first_channel).version == 5
    assert app_key not in first_hub
    assert serial not in first_device


def test_identity_normalizes_surrounding_app_key_whitespace_only() -> None:
    """Accidental surrounding whitespace must not create a duplicate hub."""
    assert hub_uid(" app-key ") == hub_uid("app-key")
    assert hub_uid("APP-KEY") != hub_uid("app-key")


def test_api_models_are_immutable_and_use_utc_datetimes() -> None:
    """Coordinator snapshots need immutable comparable model objects."""
    expires_at = datetime(2026, 9, 17, 12, tzinfo=UTC)
    token = AccessToken(token="synthetic-token", expires_at=expires_at)
    device = EzvizDevice(
        serial="SYNTHETIC-SERIAL-001",
        name="Synthetic Camera",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=AlarmSoundMode.SILENT,
        firmware_version="V1.0.0",
    )

    assert token.expires_at.tzinfo is UTC
    assert device.alarm_sound_mode is AlarmSoundMode.SILENT
    with pytest.raises(FrozenInstanceError):
        device.name = "Changed"  # type: ignore[misc]


def test_alarm_event_fingerprint_is_stable_without_alarm_id() -> None:
    """Push and poll transports need the same fallback deduplication key."""
    occurred_at = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)
    pushed = EzvizAlarmEvent(
        alarm_id=None,
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        alarm_type="motion",
        occurred_at=occurred_at,
        image_url="https://example.invalid/alarm.jpg",
        source=EventSource.PUSH,
    )
    polled = EzvizAlarmEvent(
        alarm_id=None,
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        alarm_type="motion",
        occurred_at=occurred_at,
        image_url="https://example.invalid/alarm.jpg",
        source=EventSource.POLL,
    )

    assert pushed.event_key == polled.event_key
    assert pushed.event_key.startswith("fingerprint:")


def test_calling_event_keys_include_lifecycle_status_and_avoid_alarm_collisions() -> (
    None
):
    """One call ID can advance through each documented lifecycle status."""
    occurred_at = datetime(2026, 9, 18, 9, 30, tzinfo=UTC)
    calling = EzvizCallingEvent(
        calling_id="calling-001",
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        action=CallingAction.RING,
        occurred_at=occurred_at,
        image_url="https://example.invalid/calling.jpg",
        source=EventSource.PUSH,
    )
    without_id = EzvizCallingEvent(
        calling_id=None,
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        action=CallingAction.RING,
        occurred_at=occurred_at,
        image_url="https://example.invalid/calling.jpg",
        source=EventSource.PUSH,
    )
    answered = EzvizCallingEvent(
        calling_id="calling-001",
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        action=CallingAction.ANSWER,
        occurred_at=occurred_at,
        image_url=None,
        source=EventSource.PUSH,
    )
    alarm = EzvizAlarmEvent(
        alarm_id="calling-001",
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
        alarm_type="motion",
        occurred_at=occurred_at,
        image_url=None,
        source=EventSource.PUSH,
    )

    assert calling.event_key == "calling:calling-001:1"
    assert answered.event_key == "calling:calling-001:2"
    assert calling.event_key != answered.event_key
    assert without_id.event_key.startswith("calling-fingerprint:")
    assert (
        without_id.event_key
        == EzvizCallingEvent(
            calling_id=None,
            device_serial="SYNTHETIC-SERIAL-001",
            channel_no=1,
            action=CallingAction.RING,
            occurred_at=occurred_at,
            image_url=None,
            source=EventSource.PUSH,
        ).event_key
    )
    assert calling.event_key != alarm.event_key


def test_official_control_enums_keep_documented_wire_values() -> None:
    """Control enums must not depend on translated display strings."""
    assert AlarmSoundMode.SHORT_ALERT.value == 0
    assert AlarmSoundMode.LONG_ALERT.value == 1
    assert AlarmSoundMode.SILENT.value == 2
    assert PtzDirection.UP.value == 0
    assert PtzDirection.DOWN.value == 1
    assert PtzDirection.LEFT.value == 2
    assert PtzDirection.RIGHT.value == 3
    assert CallingAction.RING.value == 1
    assert CallingAction.ANSWER.value == 2
    assert CallingAction.HANG_UP.value == 3


def test_api_package_does_not_import_home_assistant() -> None:
    """The API package must remain independently testable."""
    api_root = Path(__file__).parents[1] / "custom_components" / "ezviz_iot_cn" / "api"

    for path in api_root.glob("*.py"):
        assert "homeassistant" not in path.read_text(encoding="utf-8")
