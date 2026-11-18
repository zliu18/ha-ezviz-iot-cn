"""Strict response parsers for documented EZVIZ Open Platform data."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .errors import EzvizApiResponseError, EzvizProtocolError
from .models import (
    AccessToken,
    AlarmLightProtocol,
    AlarmSoundMode,
    EventSource,
    EzvizAlarmEvent,
    EzvizAlarmLightState,
    EzvizCapabilities,
    EzvizCapture,
    EzvizChannel,
    EzvizDevice,
    EzvizPage,
    NightVisionMode,
    NightVisionSettings,
)

JsonMapping = Mapping[str, Any]


def _success_data(payload: JsonMapping) -> Any:
    code = payload.get("code")
    if code is None:
        raise EzvizProtocolError("Response code is missing")
    if str(code) != "200":
        raise EzvizApiResponseError(str(code))
    if "data" not in payload:
        raise EzvizProtocolError("Response data is missing")
    return payload["data"]


def _mapping(value: Any, field: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise EzvizProtocolError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EzvizProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def _scalar_string(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise EzvizProtocolError(f"{field} must be a non-empty scalar")
    normalized = str(value).strip()
    if not normalized:
        raise EzvizProtocolError(f"{field} must be a non-empty scalar")
    return normalized


def _optional_string(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise EzvizProtocolError(f"{field} must be a string")
    normalized = value.strip()
    return normalized or None


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EzvizProtocolError(f"{field} must be an integer")
    return value


def _optional_integer(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    return _integer(value, field)


def _flag(value: Any) -> bool:
    return value is True or value == 1 or value == "1"


def _capability_level(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise EzvizProtocolError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as err:
        raise EzvizProtocolError(f"{field} must be a non-negative integer") from err
    if parsed < 0:
        raise EzvizProtocolError(f"{field} must be a non-negative integer")
    return parsed


def _shared_recipient(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return (isinstance(value, int) and value == 2) or (
        isinstance(value, str) and value == "2"
    )


def _optional_sound_mode(value: Any) -> AlarmSoundMode | None:
    raw = _optional_integer(value, "alarmSoundMode")
    if raw is None:
        return None
    try:
        return AlarmSoundMode(raw)
    except ValueError:
        return None


def _page(payload: JsonMapping, items: tuple[Any, ...]) -> EzvizPage[Any]:
    page_data = payload.get("page", {})
    if page_data is None:
        page_data = {}
    page_mapping = _mapping(page_data, "page")
    return EzvizPage(
        items=items,
        page=_integer(page_mapping.get("page", 0), "page.page"),
        size=_integer(page_mapping.get("size", len(items)), "page.size"),
        total=_integer(page_mapping.get("total", len(items)), "page.total"),
    )


def _utc_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, bool):
        raise EzvizProtocolError(f"{field} must be a timestamp")
    if isinstance(value, int | float) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        timestamp = float(value)
        if timestamp >= 100_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as err:
            raise EzvizProtocolError(f"{field} must be a timestamp") from err
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise EzvizProtocolError(f"{field} must be a timestamp")


def parse_token(payload: JsonMapping) -> AccessToken:
    """Parse a token response."""
    data = _mapping(_success_data(payload), "data")
    return AccessToken(
        token=_string(data.get("accessToken"), "accessToken"),
        expires_at=_utc_datetime(data.get("expireTime"), "expireTime"),
    )


def parse_device_page(payload: JsonMapping) -> EzvizPage[EzvizDevice]:
    """Parse one device-list response page."""
    data = _success_data(payload)
    if not isinstance(data, list):
        raise EzvizProtocolError("data must be a device list")
    devices: list[EzvizDevice] = []
    for index, raw in enumerate(data):
        item = _mapping(raw, f"data[{index}]")
        devices.append(
            EzvizDevice(
                serial=_string(item.get("deviceSerial"), "deviceSerial"),
                name=_string(item.get("deviceName"), "deviceName"),
                model=_string(item.get("deviceType"), "deviceType"),
                online=_flag(item.get("status")),
                defence=_optional_integer(item.get("defence"), "defence"),
                alarm_sound_mode=_optional_sound_mode(item.get("alarmSoundMode")),
                firmware_version=(
                    str(item["deviceVersion"]).strip()
                    if item.get("deviceVersion") not in (None, "")
                    else None
                ),
            )
        )
    return _page(payload, tuple(devices))


def parse_device_status_alarm_sound_mode(
    payload: JsonMapping,
) -> AlarmSoundMode | None:
    """Parse the writable alarm-sound mode from one device status response."""
    data = _mapping(_success_data(payload), "data")
    return _optional_sound_mode(data.get("alarmSoundMode"))


def parse_alarm_light_state(
    payload: JsonMapping,
    *,
    requested_protocol: AlarmLightProtocol,
) -> EzvizAlarmLightState:
    """Parse one light switch state and verify the echoed protocol."""
    data = _mapping(_success_data(payload), "data")
    raw_protocol = _integer(data.get("type"), "data.type")
    if raw_protocol != requested_protocol.value:
        raise EzvizProtocolError("Alarm-light response did not echo requested protocol")
    enabled = data.get("enable")
    if not isinstance(enabled, bool):
        raise EzvizProtocolError("data.enable must be a boolean")
    return EzvizAlarmLightState(requested_protocol, enabled)


def parse_channel_page(payload: JsonMapping) -> EzvizPage[EzvizChannel]:
    """Parse one camera/channel-list response page."""
    data = _success_data(payload)
    if not isinstance(data, list):
        raise EzvizProtocolError("data must be a channel list")
    channels: list[EzvizChannel] = []
    for index, raw in enumerate(data):
        item = _mapping(raw, f"data[{index}]")
        channels.append(
            EzvizChannel(
                device_serial=_string(item.get("deviceSerial"), "deviceSerial"),
                channel_no=_integer(item.get("channelNo"), "channelNo"),
                name=_string(item.get("channelName"), "channelName"),
                online=_flag(item.get("status")),
                encrypted=_flag(item.get("isEncrypt")),
                is_shared=_shared_recipient(item.get("isShared")),
            )
        )
    return _page(payload, tuple(channels))


def parse_capabilities(
    payload: JsonMapping,
    *,
    device_serial: str,
) -> EzvizCapabilities:
    """Parse documented device capability flags."""
    data = _mapping(_success_data(payload), "data")
    return EzvizCapabilities(
        device_serial=device_serial,
        supports_capture=_flag(data.get("support_capture")),
        supports_night_vision=_flag(data.get("support_night_vision_mode")),
        supports_smart_night_vision=_flag(data.get("support_smart_night_vision")),
        supports_defence=_flag(data.get("support_defence")),
        supports_ptz=_flag(data.get("support_ptz")),
        ptz_top_bottom=_flag(data.get("ptz_top_bottom")),
        ptz_left_right=_flag(data.get("ptz_left_right")),
        alarm_voice_level=_capability_level(
            data.get("support_alarm_voice"),
            "support_alarm_voice",
        ),
        active_defense_level=_capability_level(
            data.get("support_active_defense"),
            "support_active_defense",
        ),
        pir_detection_level=_capability_level(
            data.get("support_pir_detect"),
            "support_pir_detect",
        ),
    )


def parse_alarm_page(
    payload: JsonMapping,
    *,
    source: EventSource,
) -> EzvizPage[EzvizAlarmEvent]:
    """Parse one account-level alarm-list response page."""
    data = _success_data(payload)
    if not isinstance(data, list):
        raise EzvizProtocolError("data must be an alarm list")
    events: list[EzvizAlarmEvent] = []
    for index, raw in enumerate(data):
        item = _mapping(raw, f"data[{index}]")
        events.append(
            EzvizAlarmEvent(
                alarm_id=_optional_string(item.get("alarmId"), "alarmId"),
                device_serial=_string(item.get("deviceSerial"), "deviceSerial"),
                channel_no=_integer(item.get("channelNo"), "channelNo"),
                alarm_type=_scalar_string(item.get("alarmType"), "alarmType"),
                occurred_at=_utc_datetime(item.get("alarmTime"), "alarmTime"),
                image_url=_optional_string(item.get("alarmPicUrl"), "alarmPicUrl"),
                source=source,
            )
        )
    return _page(payload, tuple(events))


def parse_night_vision(payload: JsonMapping) -> NightVisionSettings:
    """Parse the verified NightVision_Model readback shape."""
    data = _mapping(payload.get("data"), "data")
    value = _mapping(data.get("valueInfo"), "valueInfo")
    code = _integer(value.get("graphicType"), "graphicType")
    duration = _integer(value.get("duration"), "duration")
    brightness = _integer(value.get("luminance"), "luminance")
    try:
        mode = NightVisionMode(code)
    except ValueError as err:
        raise EzvizProtocolError("Unknown night-vision mode") from err
    if not 0 <= duration <= 3600 or not 0 <= brightness <= 100:
        raise EzvizProtocolError("Invalid illumination range")
    return NightVisionSettings(mode, duration, brightness)


def parse_capture(
    payload: JsonMapping,
    *,
    device_serial: str,
    channel_no: int,
) -> EzvizCapture:
    """Parse an active capture response."""
    data = _mapping(_success_data(payload), "data")
    image_url = _string(data.get("picUrl"), "picUrl")
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EzvizProtocolError("picUrl must be an HTTPS URL")
    return EzvizCapture(
        device_serial=device_serial,
        channel_no=channel_no,
        image_url=image_url,
    )
