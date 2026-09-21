"""Near-real-time adapter for EZVIZ China Cloud Push messages."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.web import Request, Response
from homeassistant.components import persistent_notification
from homeassistant.components import webhook as ha_webhook
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations

from .api.errors import EzvizProtocolError
from .api.models import (
    CallingAction,
    EventSource,
    EzvizAlarmEvent,
    EzvizCallingEvent,
)
from .const import (
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_ID_PREFIX,
)
from .runtime import EzvizConfigEntry, WebhookStats

_LOGGER = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
MAX_WEBHOOK_STAT_COUNT = 2**31 - 1
WEBHOOK_QUEUE_SIZE = 128
WEBHOOK_READ_TIMEOUT_SECONDS = 1.5
_MAX_REGISTRATION_ATTEMPTS = 10
_RESERVED_WEBHOOK_IDS = frozenset({"ezviz-push-webhook"})
_WEBHOOK_CREATED_NOTIFICATION_KEY = "webhook_created"
# Official Open Platform Message Push feature development reference:
# https://open.ys7.com/help/5130
_ALARM_MESSAGE_TYPE = "ys.alarm"
_CALLING_MESSAGE_TYPE = "ys.calling"
_CHINA_PLATFORM_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class EzvizPushEnvelope:
    """Immutable minimal Cloud Push envelope retained in the local queue."""

    device_id: str
    channel_no: int | None
    message_id: str
    message_time: datetime | None
    message_type: str
    body: Mapping[str, Any]


async def async_register_native_webhook(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
) -> None:
    """Register one persisted, collision-resistant external webhook."""
    if not entry.options.get(CONF_ENABLE_NATIVE_WEBHOOK, False):
        return
    persisted = entry.data.get(CONF_WEBHOOK_ID)
    candidate = str(persisted) if _valid_webhook_id(persisted) else None
    generated_webhook = candidate is None
    handler = partial(async_handle_native_webhook, entry=entry)

    for _attempt in range(_MAX_REGISTRATION_ATTEMPTS):
        webhook_id = candidate or _new_webhook_id()
        try:
            ha_webhook.async_register(
                hass,
                DOMAIN,
                entry.title or "EZVIZ IoT CN",
                webhook_id,
                handler,
                local_only=False,
                allowed_methods={"POST"},
            )
        except ValueError:
            candidate = None
            continue
        break
    else:
        raise RuntimeError("Unable to allocate an EZVIZ IoT CN webhook ID")

    if entry.data.get(CONF_WEBHOOK_ID) != webhook_id:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_WEBHOOK_ID: webhook_id},
        )
    runtime = entry.runtime_data
    runtime.webhook_id = webhook_id
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(
        _async_webhook_worker(entry),
        name=f"ezviz-iot-cn-webhook-{entry.entry_id}",
    )
    entry.async_on_unload(lambda: async_unregister_native_webhook(hass, entry))
    if generated_webhook:
        await _async_notify_webhook_created(hass, entry, webhook_id)


async def _async_notify_webhook_created(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    webhook_id: str,
) -> None:
    """Show the private URL once, when this hub first receives its webhook ID."""
    try:
        webhook_url = ha_webhook.async_generate_url(hass, webhook_id)
    except HomeAssistantError:
        webhook_url = ha_webhook.async_generate_path(webhook_id)

    translations = await async_get_translations(
        hass,
        hass.config.language,
        "issues",
        {DOMAIN},
    )
    key_prefix = f"component.{DOMAIN}.issues.{_WEBHOOK_CREATED_NOTIFICATION_KEY}"
    title = translations.get(
        f"{key_prefix}.title",
        "EZVIZ IoT CN webhook created",
    )
    description = translations.get(
        f"{key_prefix}.description",
        (
            "Your private EZVIZ Cloud Push webhook URL is:\n\n"
            "`{webhook_url}`\n\n"
            "This URL is a sensitive credential. Use HTTPS, expose only its exact "
            "`/api/webhook/...` path, and do not share it."
        ),
    )
    persistent_notification.async_create(
        hass,
        description.format(webhook_url=webhook_url),
        title,
        f"{DOMAIN}_{entry.entry_id}_{_WEBHOOK_CREATED_NOTIFICATION_KEY}",
    )


async def async_unregister_native_webhook(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
) -> None:
    """Unregister ingress and cancel its local worker idempotently."""
    runtime = entry.runtime_data
    if isinstance(runtime.webhook_id, str):
        ha_webhook.async_unregister(hass, runtime.webhook_id)
        runtime.webhook_id = None
    if isinstance(runtime.webhook_task, asyncio.Task):
        runtime.webhook_task.cancel()
        await asyncio.gather(runtime.webhook_task, return_exceptions=True)
        runtime.webhook_task = None
    runtime.webhook_queue = None


async def async_handle_native_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    request: Request,
    *,
    entry: EzvizConfigEntry,
) -> Response:
    """Read a bounded callback, ACK its identity, then enqueue valid work."""
    del hass, webhook_id
    if request.method != "POST":
        return Response(status=HTTPStatus.METHOD_NOT_ALLOWED)
    try:
        async with asyncio.timeout(WEBHOOK_READ_TIMEOUT_SECONDS):
            body = await _async_read_limited(request)
    except TimeoutError:
        return Response(status=HTTPStatus.REQUEST_TIMEOUT)
    if body is None:
        return Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    try:
        payload = json.loads(body)
        message_id = _parse_ack_message_id(payload)
    except UnicodeDecodeError, json.JSONDecodeError, EzvizProtocolError:
        return Response(status=HTTPStatus.BAD_REQUEST)

    runtime = entry.runtime_data
    try:
        envelope = _parse_envelope(payload)
    except EzvizProtocolError:
        _increment(runtime.webhook_stats, "invalid_messages")
        return web.json_response({"messageId": message_id})

    _increment(runtime.webhook_stats, "valid_envelopes")
    queue = runtime.webhook_queue
    if queue is None:
        _increment(runtime.webhook_stats, "queue_drops")
    else:
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:
            previous = runtime.webhook_stats.queue_drops
            _increment(runtime.webhook_stats, "queue_drops")
            if previous == 0:
                _LOGGER.warning(
                    "EZVIZ Cloud Push queue is full; polling remains the fallback"
                )
    return web.json_response({"messageId": message_id})


async def _async_read_limited(request: Request) -> bytes | None:
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                return None
        except ValueError:
            return None
    body = bytearray()
    stream = request.content
    while True:
        chunk = await stream.read(
            min(64 * 1024, MAX_WEBHOOK_BODY_BYTES + 1 - len(body))
        )
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            return None


def _parse_envelope(payload: Any) -> EzvizPushEnvelope:
    if not isinstance(payload, Mapping):
        raise EzvizProtocolError("Cloud Push payload must be an object")
    header = payload.get("header")
    body = payload.get("body")
    if not isinstance(header, Mapping) or not isinstance(body, Mapping):
        raise EzvizProtocolError("Cloud Push header and body must be objects")
    return EzvizPushEnvelope(
        device_id=_required_string(header.get("deviceId")),
        channel_no=(
            None
            if header.get("channelNo") is None
            else _required_integer(header.get("channelNo"), minimum=0)
        ),
        message_id=_required_string(header.get("messageId")),
        message_time=_optional_milliseconds_time(header.get("messageTime")),
        message_type=_required_string(header.get("type")),
        body=MappingProxyType(
            {str(key): _freeze_json(value) for key, value in body.items()}
        ),
    )


def _parse_ack_message_id(payload: Any) -> str:
    """Return the only field required to construct the provider ACK."""
    if not isinstance(payload, Mapping):
        raise EzvizProtocolError("Cloud Push payload must be an object")
    header = payload.get("header")
    if not isinstance(header, Mapping):
        raise EzvizProtocolError("Cloud Push header must be an object")
    return _required_string(header.get("messageId"))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


async def _async_webhook_worker(entry: EzvizConfigEntry) -> None:
    queue = entry.runtime_data.webhook_queue
    if queue is None:
        return
    while True:
        envelope = await queue.get()
        try:
            await _async_process_envelope(entry, envelope)
        except Exception:  # Keep the worker alive without logging payload details.
            _increment(entry.runtime_data.webhook_stats, "worker_errors")
            _LOGGER.error("Unexpected EZVIZ Cloud Push processing failure")
        finally:
            queue.task_done()


async def _async_process_envelope(
    entry: EzvizConfigEntry,
    envelope: EzvizPushEnvelope,
) -> None:
    runtime = entry.runtime_data
    stats = runtime.webhook_stats
    if envelope.message_type not in {_ALARM_MESSAGE_TYPE, _CALLING_MESSAGE_TYPE}:
        _increment(stats, "unsupported_types")
        return
    try:
        channel_no = _message_channel_no(envelope)
    except EzvizProtocolError:
        _increment(stats, "invalid_messages")
        return
    if not _target_belongs_to_entry(entry, envelope.device_id, channel_no):
        _increment(stats, "unknown_targets")
        return

    try:
        if envelope.message_type == _ALARM_MESSAGE_TYPE:
            alarm_event = _parse_alarm_event(envelope, channel_no)
            changed = await runtime.reconciler.async_process_alarm(alarm_event)
            _increment(stats, "processed_alarms")
        else:
            calling_event = _parse_calling_event(envelope, channel_no)
            changed = await runtime.reconciler.async_process_call(calling_event)
            _increment(stats, "processed_calls")
    except EzvizProtocolError:
        _increment(stats, "invalid_messages")
        return
    if changed:
        runtime.event_store.async_schedule_save()


def _parse_alarm_event(
    envelope: EzvizPushEnvelope,
    channel_no: int,
) -> EzvizAlarmEvent:
    body = envelope.body
    alarm_id = _optional_string(body.get("alarmId"))
    alarm_type = _required_string(body.get("alarmType"))
    occurred_at = _iso_time(body.get("alarmTime"))
    return EzvizAlarmEvent(
        alarm_id=alarm_id,
        device_serial=envelope.device_id,
        channel_no=channel_no,
        alarm_type=alarm_type,
        occurred_at=occurred_at,
        image_url=_first_picture_url(body.get("pictureList")),
        source=EventSource.PUSH,
    )


def _parse_calling_event(
    envelope: EzvizPushEnvelope,
    channel_no: int,
) -> EzvizCallingEvent:
    body = envelope.body
    try:
        action = CallingAction(_required_integer(body.get("status"), minimum=1))
    except ValueError as err:
        raise EzvizProtocolError("Cloud Push calling status is invalid") from err
    calling_time = body.get("callingTime")
    timestamp = body.get("timestamp")
    if calling_time is not None:
        occurred_at = _milliseconds_time(calling_time)
    elif timestamp is not None:
        occurred_at = _iso_time(timestamp)
    else:
        raise EzvizProtocolError("Cloud Push calling time is missing")
    return EzvizCallingEvent(
        calling_id=_optional_string(body.get("callingId")),
        device_serial=envelope.device_id,
        channel_no=channel_no,
        action=action,
        occurred_at=occurred_at,
        image_url=_nested_url(body.get("coverUrl")),
        source=EventSource.PUSH,
    )


def _message_channel_no(envelope: EzvizPushEnvelope) -> int:
    header_channel = envelope.channel_no
    body_channel_value = envelope.body.get("channel")
    body_channel = (
        None
        if body_channel_value is None
        else _required_integer(body_channel_value, minimum=0)
    )
    if (
        header_channel is not None
        and body_channel is not None
        and header_channel != body_channel
    ):
        raise EzvizProtocolError("Cloud Push channel fields do not match")
    channel_no = header_channel if header_channel is not None else body_channel
    if channel_no is None:
        raise EzvizProtocolError("Cloud Push channel is missing")
    return channel_no


def _first_picture_url(value: Any) -> str | None:
    if not isinstance(value, tuple) or not value:
        return None
    return _nested_url(value[0])


def _nested_url(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _optional_https_url(value.get("url"))


def _optional_https_url(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    if not text.startswith("https://"):
        return None
    return text


def _required_string(value: Any) -> str:
    text = _optional_string(value)
    if text is None:
        raise EzvizProtocolError("Required Cloud Push field is missing")
    return text


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _required_integer(value: Any, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EzvizProtocolError("Cloud Push integer field is invalid")
    if value < minimum:
        raise EzvizProtocolError("Cloud Push integer field is out of range")
    return value


def _milliseconds_time(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EzvizProtocolError("Cloud Push millisecond time is invalid")
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as err:
        raise EzvizProtocolError("Cloud Push millisecond time is invalid") from err


def _optional_milliseconds_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _milliseconds_time(value)
    except EzvizProtocolError:
        return None


def _iso_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EzvizProtocolError("Cloud Push ISO time is invalid")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as err:
        raise EzvizProtocolError("Cloud Push ISO time is invalid") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CHINA_PLATFORM_TIMEZONE)
    return parsed.astimezone(UTC)


def _target_belongs_to_entry(
    entry: EzvizConfigEntry,
    device_id: str,
    channel_no: int,
) -> bool:
    snapshot = entry.runtime_data.metadata_coordinator.data
    return any(
        channel.device_serial == device_id and channel.channel_no == channel_no
        for channel in snapshot.channels
    )


def _increment(stats: WebhookStats, field: str) -> None:
    value = getattr(stats, field)
    setattr(stats, field, min(MAX_WEBHOOK_STAT_COUNT, value + 1))


def _new_webhook_id() -> str:
    return f"{WEBHOOK_ID_PREFIX}{ha_webhook.async_generate_id()}"


def _valid_webhook_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(WEBHOOK_ID_PREFIX)
        and value not in _RESERVED_WEBHOOK_IDS
        and len(value) > len(WEBHOOK_ID_PREFIX)
    )
