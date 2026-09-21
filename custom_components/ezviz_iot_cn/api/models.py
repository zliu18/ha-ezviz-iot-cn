"""Immutable data models for the EZVIZ China Open Platform."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from hashlib import sha256
from uuid import UUID, uuid5

_PROJECT_NAMESPACE = UUID("a631cab7-4c7f-5f70-845a-8747d08a93ad")


def hub_uid(app_key: str) -> str:
    """Return a stable non-reversible identifier for an Open Platform AppKey."""
    return str(uuid5(_PROJECT_NAMESPACE, app_key.strip()))


def device_uid(hub_id: str, device_serial: str) -> str:
    """Return a stable hub-scoped identifier for a physical EZVIZ device."""
    return str(uuid5(UUID(hub_id), device_serial.strip()))


def channel_uid(device_id: str, channel_no: int) -> str:
    """Return a stable device-scoped identifier for an EZVIZ channel."""
    return str(uuid5(UUID(device_id), str(channel_no)))


class AlarmSoundMode(IntEnum):
    """Documented device alarm sound values."""

    SHORT_ALERT = 0
    LONG_ALERT = 1
    SILENT = 2


class AlarmLightProtocol(IntEnum):
    """Documented alarm-light switch protocol values."""

    MOTION = 301
    PIR = 305


class PtzDirection(IntEnum):
    """Documented four-direction PTZ wire values."""

    UP = 0
    DOWN = 1
    LEFT = 2
    RIGHT = 3


class CallingAction(IntEnum):
    """Documented EZVIZ calling lifecycle status values."""

    RING = 1
    ANSWER = 2
    HANG_UP = 3


class EventSource(StrEnum):
    """Transport that supplied a normalized alarm event."""

    PUSH = "push"
    POLL = "poll"


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Access token and absolute expiry."""

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EzvizPage[T]:
    """One normalized page returned by the Open Platform."""

    items: tuple[T, ...]
    page: int
    size: int
    total: int


@dataclass(frozen=True, slots=True)
class EzvizDevice:
    """Account-level EZVIZ device state."""

    serial: str
    name: str
    model: str | None
    online: bool
    defence: int | None
    alarm_sound_mode: AlarmSoundMode | None
    firmware_version: str | None
    is_shared: bool = False
    alarm_light_enabled: bool | None = None
    alarm_light_protocol: AlarmLightProtocol | None = None


@dataclass(frozen=True, slots=True)
class EzvizChannel:
    """One camera or recorder channel."""

    device_serial: str
    channel_no: int
    name: str
    online: bool
    encrypted: bool
    is_shared: bool = False


@dataclass(frozen=True, slots=True)
class EzvizCapabilities:
    """Normalized device capability flags."""

    device_serial: str
    supports_capture: bool
    supports_defence: bool
    supports_ptz: bool
    ptz_top_bottom: bool
    ptz_left_right: bool
    alarm_voice_level: int | None = None
    active_defense_level: int | None = None
    pir_detection_level: int | None = None

    @property
    def supports_alarm_sound(self) -> bool:
        """Return whether the capability set reports alarm-voice support."""
        return self.alarm_voice_level is not None and self.alarm_voice_level > 0

    @property
    def supports_alarm_light(self) -> bool:
        """Return whether active defence includes a light-reminder switch."""
        return self.active_defense_level == 2


@dataclass(frozen=True, slots=True)
class EzvizAlarmLightState:
    """Verified alarm-light state for one exact public API protocol."""

    protocol: AlarmLightProtocol
    enabled: bool


@dataclass(frozen=True, slots=True)
class EzvizAlarmEvent:
    """Transport-independent alarm event."""

    alarm_id: str | None
    device_serial: str
    channel_no: int
    alarm_type: str
    occurred_at: datetime
    image_url: str | None
    source: EventSource

    @property
    def event_key(self) -> str:
        """Return an official alarm key or a deterministic fallback fingerprint."""
        if self.alarm_id:
            return f"alarm:{self.alarm_id}"
        fingerprint = "|".join(
            (
                self.device_serial,
                str(self.channel_no),
                self.alarm_type,
                self.occurred_at.isoformat(),
            )
        )
        return f"fingerprint:{sha256(fingerprint.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class EzvizCallingEvent:
    """Normalized Cloud Push calling event."""

    calling_id: str | None
    device_serial: str
    channel_no: int
    action: CallingAction
    occurred_at: datetime
    image_url: str | None
    source: EventSource

    @property
    def event_key(self) -> str:
        """Return an ID/status key or deterministic fallback."""
        if self.calling_id:
            return f"calling:{self.calling_id}:{self.action.value}"
        fingerprint = "|".join(
            (
                self.device_serial,
                str(self.channel_no),
                str(self.action.value),
                self.occurred_at.isoformat(),
            )
        )
        return f"calling-fingerprint:{sha256(fingerprint.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class EzvizCapture:
    """Result of an active still-image capture request."""

    device_serial: str
    channel_no: int
    image_url: str
