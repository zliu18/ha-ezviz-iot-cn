"""Tests for persisted event-derived entity semantics."""

from __future__ import annotations

from uuid import UUID

from custom_components.ezviz_iot_cn.api.models import channel_uid, device_uid
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)

HUB_ID = "8ab35d94-a5ec-5c8a-a5bc-48e8e293722b"
SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"


def _channel_id(serial: str, channel_no: int) -> str:
    return channel_uid(device_uid(HUB_ID, serial), channel_no)


def test_observe_adds_only_new_semantics_and_notifies_once() -> None:
    """Repeated events cannot duplicate entities or discovery notifications."""
    registry = ObservedEntityRegistry(HUB_ID)
    notifications = 0

    def observed() -> None:
        nonlocal notifications
        notifications += 1

    registry.subscribe(observed)

    assert (
        registry.observe(
            SERIAL_1,
            1,
            {ObservedSemantic.ALARM, ObservedSemantic.MOTION},
        )
        is True
    )
    assert (
        registry.observe(
            SERIAL_1,
            1,
            {ObservedSemantic.MOTION},
        )
        is False
    )
    assert notifications == 1
    assert registry.keys_for(SERIAL_1, 1) == frozenset(
        {ObservedSemantic.ALARM, ObservedSemantic.MOTION}
    )
    assert registry.has(SERIAL_1, 1, ObservedSemantic.MOTION) is True
    assert registry.has(SERIAL_1, 1, ObservedSemantic.PERSON) is False


def test_channel_semantics_are_isolated_by_stable_identity() -> None:
    """One physical channel cannot grant entities to another channel/device."""
    registry = ObservedEntityRegistry(HUB_ID)

    registry.observe(SERIAL_1, 1, {ObservedSemantic.PERSON})
    registry.observe(SERIAL_1, 2, {ObservedSemantic.MOTION})
    registry.observe(SERIAL_2, 1, {ObservedSemantic.CALLING})

    assert registry.keys_for(SERIAL_1, 1) == frozenset({ObservedSemantic.PERSON})
    assert registry.keys_for(SERIAL_1, 2) == frozenset({ObservedSemantic.MOTION})
    assert registry.keys_for(SERIAL_2, 1) == frozenset({ObservedSemantic.CALLING})


def test_export_and_restore_use_only_uuid_keys_and_known_semantics() -> None:
    """Persistence restores entity evidence without raw device identifiers."""
    original = ObservedEntityRegistry(HUB_ID)
    original.observe(
        SERIAL_1,
        1,
        {ObservedSemantic.ALARM, ObservedSemantic.PERSON},
    )

    payload = original.export_state()

    assert payload == {
        "version": 1,
        "channels": {
            _channel_id(SERIAL_1, 1): ["alarm", "person"],
        },
    }
    assert SERIAL_1 not in repr(payload)
    assert HUB_ID not in repr(payload)
    UUID(next(iter(payload["channels"])))

    payload["channels"]["not-a-uuid"] = ["motion"]
    payload["channels"][_channel_id(SERIAL_2, 1)] = [
        "calling",
        "unsupported-semantic",
    ]
    restored = ObservedEntityRegistry(HUB_ID)

    restored.restore_state(payload)

    assert restored.keys_for(SERIAL_1, 1) == frozenset(
        {ObservedSemantic.ALARM, ObservedSemantic.PERSON}
    )
    assert restored.keys_for(SERIAL_2, 1) == frozenset({ObservedSemantic.CALLING})


def test_restore_rejects_an_unrelated_schema_version() -> None:
    """The unreleased format is strict instead of carrying compatibility code."""
    registry = ObservedEntityRegistry(HUB_ID)

    registry.restore_state(
        {
            "version": 0,
            "channels": {_channel_id(SERIAL_1, 1): ["motion"]},
        }
    )

    assert registry.keys_for(SERIAL_1, 1) == frozenset()
