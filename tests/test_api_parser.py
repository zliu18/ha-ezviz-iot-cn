"""Tests for strict EZVIZ API response parsing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.ezviz_iot_cn.api import parser as api_parser
from custom_components.ezviz_iot_cn.api.errors import (
    EzvizApiResponseError,
    EzvizProtocolError,
)
from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    AlarmSoundMode,
    EventSource,
)
from custom_components.ezviz_iot_cn.api.parser import (
    parse_alarm_page,
    parse_capabilities,
    parse_capture,
    parse_channel_page,
    parse_device_page,
    parse_token,
)


def test_parse_token_converts_milliseconds_to_utc() -> None:
    """The China API token expiry is normalized to a UTC datetime."""
    token = parse_token(
        {
            "code": "200",
            "msg": "Operation succeeded",
            "data": {
                "accessToken": "synthetic-token",
                "expireTime": 1789646400000,
            },
        }
    )

    assert token.token == "synthetic-token"
    assert token.expires_at == datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_parse_device_page_normalizes_documented_fields() -> None:
    """Device page parsing drops unknown and sensitive response fields."""
    page = parse_device_page(
        {
            "code": "200",
            "data": [
                {
                    "deviceSerial": "SYNTHETIC-SERIAL-001",
                    "deviceName": "Courtyard",
                    "deviceType": "CS-SYNTHETIC",
                    "status": 1,
                    "defence": 1,
                    "alarmSoundMode": 2,
                    "deviceVersion": "V1.2.3",
                    "unknownPrivateField": "discard-me",
                }
            ],
            "page": {"total": 1, "page": 0, "size": 50},
        }
    )

    assert page.page == 0
    assert page.size == 50
    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0].online is True
    assert page.items[0].defence == 1
    assert page.items[0].alarm_sound_mode is AlarmSoundMode.SILENT
    assert not hasattr(page.items[0], "unknownPrivateField")


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        (0, AlarmSoundMode.SHORT_ALERT),
        (1, AlarmSoundMode.LONG_ALERT),
        (2, AlarmSoundMode.SILENT),
        (-1, None),
    ],
)
def test_parse_device_status_alarm_sound_mode_accepts_only_writable_modes(
    raw_mode: int,
    expected: AlarmSoundMode | None,
) -> None:
    """Unsupported status values must never create a writable entity."""
    mode = api_parser.parse_device_status_alarm_sound_mode(
        {"code": "200", "data": {"alarmSoundMode": raw_mode}}
    )

    assert mode is expected


def test_parse_channel_page_supports_multiple_channels() -> None:
    """Channels retain their serial and numeric channel identity."""
    page = parse_channel_page(
        {
            "code": "200",
            "data": [
                {
                    "deviceSerial": "SYNTHETIC-SERIAL-001",
                    "channelNo": 1,
                    "channelName": "Front",
                    "status": 1,
                    "isEncrypt": 1,
                },
                {
                    "deviceSerial": "SYNTHETIC-SERIAL-001",
                    "channelNo": 2,
                    "channelName": "Rear",
                    "status": 0,
                    "isEncrypt": 0,
                },
            ],
            "page": {"total": 2, "page": 0, "size": 50},
        }
    )

    assert [item.channel_no for item in page.items] == [1, 2]
    assert [item.online for item in page.items] == [True, False]
    assert [item.encrypted for item in page.items] == [True, False]


def test_parse_channel_page_preserves_shared_device_evidence() -> None:
    """Removing isShared parsing would make the inclusion option ineffective."""
    page = parse_channel_page(
        {
            "code": "200",
            "data": [
                {
                    "deviceSerial": "SYNTHETIC-SHARED-SERIAL-001",
                    "channelNo": 1,
                    "channelName": "Shared entrance",
                    "status": 1,
                    "isEncrypt": 1,
                    "isShared": 2,
                    "permission": "1,2,3",
                }
            ],
            "page": {"total": 1, "page": 0, "size": 50},
        }
    )

    assert page.items[0].is_shared is True


def test_parse_channel_page_does_not_treat_share_owner_as_shared_recipient() -> None:
    """The official value 1 identifies a sharing owner, not a recipient."""
    page = parse_channel_page(
        {
            "code": "200",
            "data": [
                {
                    "deviceSerial": "SYNTHETIC-OWNED-SERIAL-001",
                    "channelNo": 1,
                    "channelName": "Owned entrance",
                    "status": 1,
                    "isEncrypt": 1,
                    "isShared": 1,
                }
            ],
            "page": {"total": 1, "page": 0, "size": 50},
        }
    )

    assert page.items[0].is_shared is False


def test_parse_capabilities_accepts_string_flags() -> None:
    """Only verified capability flags are promoted into public behavior."""
    capabilities = parse_capabilities(
        {
            "code": "200",
            "data": {
                "support_capture": "1",
                "support_defence": "1",
                "support_alarm_voice": "3",
                "support_active_defense": "2",
                "support_pir_detect": "1",
                "support_ptz": "1",
                "ptz_top_bottom": "1",
                "ptz_left_right": "0",
                "support_human_detection": "1",
            },
        },
        device_serial="SYNTHETIC-SERIAL-001",
    )

    assert capabilities.supports_capture is True
    assert capabilities.supports_defence is True
    assert capabilities.supports_ptz is True
    assert capabilities.ptz_top_bottom is True
    assert capabilities.ptz_left_right is False
    assert capabilities.alarm_voice_level == 3
    assert capabilities.active_defense_level == 2
    assert capabilities.pir_detection_level == 1
    assert capabilities.supports_alarm_sound is True
    assert capabilities.supports_alarm_light is True
    assert not hasattr(capabilities, "supports_person_detection")


@pytest.mark.parametrize(
    ("requested", "raw_type", "enabled"),
    [
        (AlarmLightProtocol.MOTION, 301, True),
        (AlarmLightProtocol.PIR, 305, False),
    ],
)
def test_parse_alarm_light_state_requires_exact_protocol_echo(
    requested: AlarmLightProtocol,
    raw_type: int,
    enabled: bool,
) -> None:
    """A generic success response cannot verify the wrong light protocol."""
    state = api_parser.parse_alarm_light_state(
        {
            "code": "200",
            "data": {"type": raw_type, "enable": enabled},
        },
        requested_protocol=requested,
    )

    assert state.protocol is requested
    assert state.enabled is enabled

    other = (
        AlarmLightProtocol.PIR
        if requested is AlarmLightProtocol.MOTION
        else AlarmLightProtocol.MOTION
    )
    with pytest.raises(EzvizProtocolError, match="requested protocol"):
        api_parser.parse_alarm_light_state(
            {
                "code": "200",
                "data": {"type": other.value, "enable": enabled},
            },
            requested_protocol=requested,
        )


def test_parse_alarm_page_normalizes_time_and_source() -> None:
    """Alarm list records become transport-independent events."""
    page = parse_alarm_page(
        {
            "code": "200",
            "data": [
                {
                    "alarmId": "alarm-001",
                    "deviceSerial": "SYNTHETIC-SERIAL-001",
                    "channelNo": 1,
                    "alarmType": 15010,
                    "alarmTime": 1789648200000,
                    "alarmPicUrl": "https://example.invalid/alarm.jpg",
                }
            ],
            "page": {"total": 1, "page": 0, "size": 50},
        },
        source=EventSource.POLL,
    )

    event = page.items[0]
    assert event.alarm_id == "alarm-001"
    assert event.alarm_type == "15010"
    assert event.occurred_at == datetime(2026, 9, 17, 12, 30, tzinfo=UTC)
    assert event.source is EventSource.POLL
    assert event.event_key == "alarm:alarm-001"


def test_parse_alarm_page_rejects_undocumented_legacy_field_guesses() -> None:
    """Current alarm parsing does not guess old cameraNo/alarmStart aliases."""
    with pytest.raises(EzvizProtocolError):
        parse_alarm_page(
            {
                "code": "200",
                "data": [
                    {
                        "alarmId": "alarm-legacy-fields",
                        "deviceSerial": "SYNTHETIC-SERIAL-001",
                        "cameraNo": 2,
                        "alarmType": 10002,
                        "alarmStart": 1789648200000,
                    }
                ],
                "page": {"total": 1, "page": 0, "size": 50},
            },
            source=EventSource.POLL,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channelNo", 1.5),
        ("alarmId", 12345),
        ("alarmPicUrl", 12345),
    ],
)
def test_parse_alarm_page_rejects_documented_field_type_mismatches(
    field: str,
    value: object,
) -> None:
    """Documented alarm fields are not repaired through implicit coercion."""
    alarm = {
        "alarmId": "alarm-typed",
        "deviceSerial": "SYNTHETIC-SERIAL-001",
        "channelNo": 1,
        "alarmType": 10002,
        "alarmTime": 1789648200000,
        "alarmPicUrl": "https://example.invalid/alarm.jpg",
    }
    alarm[field] = value

    with pytest.raises(EzvizProtocolError):
        parse_alarm_page(
            {
                "code": "200",
                "data": [alarm],
                "page": {"total": 1, "page": 0, "size": 50},
            },
            source=EventSource.POLL,
        )


def test_parse_capture_requires_a_valid_https_url() -> None:
    """Capture parsing rejects missing and insecure URLs."""
    capture = parse_capture(
        {"code": "200", "data": {"picUrl": "https://example.invalid/c.jpg"}},
        device_serial="SYNTHETIC-SERIAL-001",
        channel_no=1,
    )

    assert capture.image_url == "https://example.invalid/c.jpg"
    with pytest.raises(EzvizProtocolError):
        parse_capture(
            {"code": "200", "data": {"picUrl": "http://example.invalid/c.jpg"}},
            device_serial="SYNTHETIC-SERIAL-001",
            channel_no=1,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"code": "200", "data": None},
        {"code": "200", "data": [{"deviceName": "Missing serial"}]},
    ],
)
def test_parse_device_page_rejects_malformed_success_payloads(
    payload: dict[str, object],
) -> None:
    """Malformed successful responses must not fabricate device state."""
    with pytest.raises(EzvizProtocolError):
        parse_device_page(payload)


def test_api_error_does_not_repeat_remote_message() -> None:
    """Potentially sensitive server messages are not placed in exceptions."""
    secret = "synthetic-secret-that-must-not-leak"

    with pytest.raises(EzvizApiResponseError) as caught:
        parse_device_page({"code": "10005", "msg": secret, "data": None})

    assert caught.value.code == "10005"
    assert secret not in str(caught.value)
