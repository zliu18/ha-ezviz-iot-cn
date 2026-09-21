"""Tests for the bounded EZVIZ China Cloud Push adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components import webhook as ha_webhook
from homeassistant.util.aiohttp import MockRequest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn.api.models import (
    AlarmSoundMode,
    CallingAction,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    hub_uid,
)
from custom_components.ezviz_iot_cn.const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_ID_PREFIX,
)
from custom_components.ezviz_iot_cn.coordinator import MetadataSnapshot
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.runtime import WebhookStats
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)
from custom_components.ezviz_iot_cn.webhook import (
    MAX_WEBHOOK_BODY_BYTES,
    MAX_WEBHOOK_STAT_COUNT,
    WEBHOOK_QUEUE_SIZE,
    _async_webhook_worker,
    async_handle_native_webhook,
    async_register_native_webhook,
    async_unregister_native_webhook,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
SERIAL = "SYNTHETIC-SERIAL-001"
MESSAGE_ID = "synthetic-message-001"


def _device() -> EzvizDevice:
    return EzvizDevice(
        serial=SERIAL,
        name="Synthetic camera",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=AlarmSoundMode.SILENT,
        firmware_version="V1.0.0",
    )


def _entry(*, webhook_id: str | None = None) -> MockConfigEntry:
    data = {
        CONF_APP_KEY: "synthetic-app-key",
        CONF_APP_SECRET: "synthetic-secret",
    }
    if webhook_id is not None:
        data[CONF_WEBHOOK_ID] = webhook_id
    return MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic EZVIZ hub",
        data=data,
        options={CONF_ENABLE_NATIVE_WEBHOOK: True},
        unique_id=hub_uid("synthetic-app-key"),
    )


def _install_runtime(entry: MockConfigEntry) -> SimpleNamespace:
    observed_registry = ObservedEntityRegistry(entry.unique_id)
    reconciler = EventReconciler(
        observed_registry,
        now=lambda: NOW,
        hold_seconds=300,
    )
    runtime = SimpleNamespace(
        metadata_coordinator=SimpleNamespace(
            data=MetadataSnapshot(
                devices=(_device(),),
                channels=(
                    EzvizChannel(
                        device_serial=SERIAL,
                        channel_no=1,
                        name="Channel 1",
                        online=True,
                        encrypted=True,
                    ),
                ),
                capabilities=(
                    EzvizCapabilities(
                        device_serial=SERIAL,
                        supports_capture=True,
                        supports_defence=True,
                        supports_ptz=False,
                        ptz_top_bottom=False,
                        ptz_left_right=False,
                    ),
                ),
            )
        ),
        reconciler=reconciler,
        observed_registry=observed_registry,
        event_store=SimpleNamespace(async_schedule_save=Mock()),
        client=AsyncMock(),
        webhook_id=None,
        webhook_queue=None,
        webhook_task=None,
        webhook_stats=WebhookStats(),
    )
    entry.runtime_data = cast(Any, runtime)
    return runtime


def _request(
    payload: bytes,
    *,
    method: str = "POST",
    content_type: str = "application/json",
) -> MockRequest:
    return MockRequest(
        payload,
        "synthetic-test",
        method=method,
        headers={"Content-Type": content_type},
        remote="203.0.113.10",
    )


_CHANNEL_UNSET = object()


def _envelope(
    message_type: str,
    body: dict[str, Any],
    *,
    message_id: str = MESSAGE_ID,
    device_id: str = SERIAL,
    channel_no: Any = 1,
) -> bytes:
    header = {
        "deviceId": device_id,
        "messageId": message_id,
        "messageTime": NOW_MS,
        "type": message_type,
    }
    if channel_no is not _CHANNEL_UNSET:
        header["channelNo"] = channel_no
    return json.dumps({"header": header, "body": body}).encode()


def _alarm_envelope(**changes: Any) -> bytes:
    body: dict[str, Any] = {
        "alarmId": "synthetic-alarm-1",
        "alarmType": "human_detection",
        "alarmTime": "2026-09-17T12:00:00Z",
        "pictureList": [
            {"id": "synthetic-picture", "url": "https://example.invalid/alarm.jpg"}
        ],
    }
    body.update(changes)
    return _envelope("ys.alarm", body)


def _calling_envelope(*, status: int = 1, **changes: Any) -> bytes:
    body: dict[str, Any] = {
        "callingId": "synthetic-calling-1",
        "action": 2,
        "status": status,
        "callingTime": NOW_MS,
        "timestamp": "2026-09-17T12:00:00Z",
        "coverUrl": {"url": "https://example.invalid/calling.jpg"},
    }
    body.update(changes)
    return _envelope("ys.calling", body)


def _assert_ack(response: Any, message_id: str = MESSAGE_ID) -> None:
    assert response.status == HTTPStatus.OK
    assert response.content_type == "application/json"
    assert json.loads(response.text) == {"messageId": message_id}


@pytest.mark.parametrize(
    ("language", "sensitive_label"),
    (("en", "sensitive credential"), ("zh-Hans", "敏感凭据")),
)
@pytest.mark.asyncio
async def test_first_registration_notifies_user_of_private_webhook_url(
    hass,
    enable_custom_integrations,
    language: str,
    sensitive_label: str,
) -> None:
    """Generating the first webhook URL creates one persistent notification."""
    hass.config.language = language
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = _install_runtime(entry)
    webhook_id = f"{WEBHOOK_ID_PREFIX}first"
    webhook_url = f"https://ha.example.test/api/webhook/{webhook_id}"

    with (
        patch(
            "custom_components.ezviz_iot_cn.webhook.ha_webhook.async_generate_id",
            return_value="first",
        ),
        patch(
            "custom_components.ezviz_iot_cn.webhook.ha_webhook.async_generate_url",
            return_value=webhook_url,
        ),
        patch(
            "custom_components.ezviz_iot_cn.webhook.persistent_notification.async_create"
        ) as create_notification,
    ):
        await async_register_native_webhook(hass, entry)

    create_notification.assert_called_once()
    notification = create_notification.call_args
    assert notification.args[0] is hass
    assert webhook_url in notification.args[1]
    assert sensitive_label in notification.args[1]
    assert notification.args[3] == f"{DOMAIN}_{entry.entry_id}_webhook_created"

    await async_unregister_native_webhook(hass, entry)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_registration_with_persisted_webhook_does_not_notify_again(hass) -> None:
    """Reloading a previously generated webhook does not repeat the notice."""
    webhook_id = f"{WEBHOOK_ID_PREFIX}stable"
    entry = _entry(webhook_id=webhook_id)
    entry.add_to_hass(hass)
    runtime = _install_runtime(entry)

    with patch.object(persistent_notification, "async_create") as create_notification:
        await async_register_native_webhook(hass, entry)

    create_notification.assert_not_called()

    await async_unregister_native_webhook(hass, entry)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_registration_regenerates_collision_and_persists_stable_id(hass) -> None:
    """The native webhook uses a private namespace and survives reloads."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = _install_runtime(entry)

    with (
        patch(
            "custom_components.ezviz_iot_cn.webhook.ha_webhook.async_generate_id",
            side_effect=("collision", "valid"),
        ),
        patch(
            "custom_components.ezviz_iot_cn.webhook.ha_webhook.async_register",
            side_effect=(ValueError("collision"), None),
        ) as register,
        patch(
            "custom_components.ezviz_iot_cn.webhook.ha_webhook.async_unregister"
        ) as unregister,
    ):
        await async_register_native_webhook(hass, entry)
        first_id = register.call_args_list[0].args[3]
        second_id = register.call_args_list[1].args[3]
        assert first_id == f"{WEBHOOK_ID_PREFIX}collision"
        assert second_id == f"{WEBHOOK_ID_PREFIX}valid"
        assert second_id != "ezviz-push-webhook"
        assert register.call_args_list[1].kwargs["allowed_methods"] == {"POST"}
        assert entry.data[CONF_WEBHOOK_ID] == second_id
        assert runtime.webhook_id == second_id

        await async_unregister_native_webhook(hass, entry)
        unregister.assert_called_once_with(hass, second_id)

    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_valid_alarm_is_acked_before_blocked_worker_processing(hass) -> None:
    """Ingress returns the provider ACK without waiting for event processing."""
    webhook_id = f"{WEBHOOK_ID_PREFIX}stable"
    entry = _entry(webhook_id=webhook_id)
    entry.add_to_hass(hass)
    runtime = _install_runtime(entry)
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    original_process = runtime.reconciler.async_process_alarm

    async def blocked_process(event: Any) -> bool:
        worker_started.set()
        await release_worker.wait()
        return await original_process(event)

    runtime.reconciler.async_process_alarm = blocked_process
    await async_register_native_webhook(hass, entry)

    get_response = await ha_webhook.async_handle_webhook(
        hass,
        webhook_id,
        _request(b"", method="GET"),
    )
    response = await ha_webhook.async_handle_webhook(
        hass,
        webhook_id,
        _request(_alarm_envelope()),
    )

    assert get_response.status == HTTPStatus.METHOD_NOT_ALLOWED
    _assert_ack(response)
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    assert runtime.reconciler.alarm_state_for(SERIAL, 1) is None
    assert runtime.event_store.async_schedule_save.call_count == 0

    release_worker.set()
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.last_alarm_id == "synthetic-alarm-1"
    assert state.last_alarm_type == "human_detection"
    assert state.last_alarm_time == NOW
    assert state.person is True
    assert runtime.webhook_stats.valid_envelopes == 1
    assert runtime.webhook_stats.processed_alarms == 1
    runtime.event_store.async_schedule_save.assert_called_once_with()
    runtime.client.assert_not_called()

    await async_unregister_native_webhook(hass, entry)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_intelligent_detection_alarm_establishes_motion_support(hass) -> None:
    """The documented C8C intelligent-detection type creates motion state."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(_alarm_envelope(alarmType="15010")),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.motion is True
    assert state.person is False
    assert runtime.observed_registry.keys_for(SERIAL, 1) == frozenset(
        {
            ObservedSemantic.ALARM,
            ObservedSemantic.MOTION,
            ObservedSemantic.ALARM_IMAGE,
        }
    )

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_calling_ring_is_normalized_without_cloud_io(hass) -> None:
    """A documented calling message updates calling and doorbell state."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(_calling_envelope()),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.calling_state_for(SERIAL, 1)
    assert state is not None
    assert state.action is CallingAction.RING
    assert state.ringing is True
    assert state.latest_calling_image_url == "https://example.invalid/calling.jpg"
    assert runtime.webhook_stats.processed_calls == 1
    runtime.client.assert_not_called()

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (1, CallingAction.RING),
        (2, CallingAction.ANSWER),
        (3, CallingAction.HANG_UP),
    ],
)
async def test_calling_uses_documented_status_not_delivery_action(
    hass,
    status: int,
    expected: CallingAction,
) -> None:
    """Calling lifecycle comes from status; action is delivery handling."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(_calling_envelope(status=status, action=0)),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.calling_state_for(SERIAL, 1)
    assert state is not None
    assert state.action is expected

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_official_body_channel_is_used_when_header_channel_is_absent(
    hass,
) -> None:
    """The documented body.channel routes messages without optional channelNo."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))
    payload = _envelope(
        "ys.alarm",
        {
            "alarmId": "synthetic-alarm-body-channel",
            "alarmType": "human_detection",
            "alarmTime": "2026-09-17T12:00:00Z",
            "channel": 1,
            "pictureList": [],
        },
        channel_no=_CHANNEL_UNSET,
    )

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(payload),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.last_alarm_id == "synthetic-alarm-body-channel"

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_worker_continues_after_unexpected_message_processing_error(hass) -> None:
    """One internal processing error cannot permanently stop Push delivery."""
    entry = _entry()
    runtime = _install_runtime(entry)
    original = runtime.reconciler.async_process_alarm
    calls = 0

    async def fail_once(event: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic failure")
        return await original(event)

    runtime.reconciler.async_process_alarm = fail_once
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    for message_id in ("failed-message", "successful-message"):
        response = await async_handle_native_webhook(
            hass,
            f"{WEBHOOK_ID_PREFIX}test",
            _request(
                _envelope(
                    "ys.alarm",
                    {
                        "alarmId": message_id,
                        "alarmType": "human_detection",
                        "alarmTime": "2026-09-17T12:00:00Z",
                        "pictureList": [],
                    },
                    message_id=message_id,
                )
            ),
            entry=entry,
        )
        _assert_ack(response, message_id)

    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    assert runtime.webhook_task.done() is False
    assert runtime.webhook_stats.worker_errors == 1
    assert runtime.webhook_stats.processed_alarms == 1

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "counter"),
    [
        (_envelope("ys.unhandled", {"synthetic": True}), "unsupported_types"),
        (_envelope("ys.alarm", {}, device_id="UNKNOWN-SERIAL"), "unknown_targets"),
        (_envelope("ys.alarm", {}), "invalid_messages"),
        (_envelope("ys.calling", {"action": 0, "status": 99}), "invalid_messages"),
    ],
)
async def test_valid_envelopes_are_acked_when_background_message_is_ignored(
    hass,
    payload: bytes,
    counter: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Local routing limits never trigger provider retries or leak payload data."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))
    caplog.set_level(logging.DEBUG)

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(payload),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    assert getattr(runtime.webhook_stats, counter) == 1
    assert runtime.reconciler.alarm_state_for(SERIAL, 1) is None
    assert runtime.reconciler.calling_state_for(SERIAL, 1) is None
    assert "UNKNOWN-SERIAL" not in caplog.text
    assert MESSAGE_ID not in caplog.text
    assert "example.invalid" not in caplog.text

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_json_payload_accepts_generic_content_type(hass) -> None:
    """A valid JSON callback is processed even when the MIME type is generic."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    try:
        response = await async_handle_native_webhook(
            hass,
            f"{WEBHOOK_ID_PREFIX}test",
            _request(_alarm_envelope(), content_type="text/plain"),
            entry=entry,
        )

        _assert_ack(response)
        await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
        assert runtime.webhook_stats.processed_alarms == 1
    finally:
        runtime.webhook_task.cancel()
        await asyncio.gather(runtime.webhook_task, return_exceptions=True)
        await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_missing_message_time_does_not_block_alarm_processing(hass) -> None:
    """The provider timestamp is optional because event bodies carry their time."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))
    payload = json.loads(_alarm_envelope())
    del payload["header"]["messageTime"]

    try:
        response = await async_handle_native_webhook(
            hass,
            f"{WEBHOOK_ID_PREFIX}test",
            _request(json.dumps(payload).encode()),
            entry=entry,
        )

        _assert_ack(response)
        await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
        assert runtime.webhook_stats.processed_alarms == 1
    finally:
        runtime.webhook_task.cancel()
        await asyncio.gather(runtime.webhook_task, return_exceptions=True)
        await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_invalid_message_time_does_not_block_alarm_processing(hass) -> None:
    """Unused provider timestamps never prevent valid event-body processing."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))
    payload = json.loads(_alarm_envelope())
    payload["header"]["messageTime"] = str(NOW_MS)

    try:
        response = await async_handle_native_webhook(
            hass,
            f"{WEBHOOK_ID_PREFIX}test",
            _request(json.dumps(payload).encode()),
            entry=entry,
        )

        _assert_ack(response)
        await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
        assert runtime.webhook_stats.processed_alarms == 1
    finally:
        runtime.webhook_task.cancel()
        await asyncio.gather(runtime.webhook_task, return_exceptions=True)
        await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_ack_identity_is_returned_before_full_envelope_validation(hass) -> None:
    """Message-specific envelope defects are acknowledged and ignored locally."""
    entry = _entry()
    runtime = _install_runtime(entry)
    payload = {
        "header": {"messageId": MESSAGE_ID},
        "body": {},
    }

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(json.dumps(payload).encode()),
        entry=entry,
    )

    _assert_ack(response)
    assert runtime.webhook_stats.valid_envelopes == 0
    assert runtime.webhook_stats.invalid_messages == 1
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mock_request", "status"),
    [
        (_request(b"{"), HTTPStatus.BAD_REQUEST),
        (
            _request(b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)),
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        ),
        (_request(json.dumps({"body": {}}).encode()), HTTPStatus.BAD_REQUEST),
        (
            _request(
                json.dumps(
                    {
                        "header": {
                            "deviceId": SERIAL,
                            "type": "ys.alarm",
                            "messageId": "",
                        },
                        "body": {},
                    }
                ).encode()
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            _request(
                json.dumps(
                    {
                        "header": {
                            "deviceId": SERIAL,
                            "channelNo": 1,
                            "type": "ys.alarm",
                            "messageId": 12345,
                            "messageTime": NOW_MS,
                        },
                        "body": {},
                    }
                ).encode()
            ),
            HTTPStatus.BAD_REQUEST,
        ),
    ],
)
async def test_request_safety_rejects_malformed_envelopes(
    hass,
    mock_request: MockRequest,
    status: HTTPStatus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requests without a valid provider ACK identity are rejected."""
    entry = _entry()
    runtime = _install_runtime(entry)
    caplog.set_level(logging.DEBUG)

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        mock_request,
        entry=entry,
    )

    assert response.status == status
    assert runtime.webhook_stats.valid_envelopes == 0
    assert MESSAGE_ID not in caplog.text
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_naive_official_iso_time_uses_china_platform_timezone(hass) -> None:
    """CN Push timestamps without an offset are interpreted in Asia/Shanghai."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
    runtime.webhook_task = asyncio.create_task(_async_webhook_worker(entry))

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(_alarm_envelope(alarmTime="2026-09-17T12:00:00")),
        entry=entry,
    )

    _assert_ack(response)
    await asyncio.wait_for(runtime.webhook_queue.join(), timeout=1)
    state = runtime.reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.last_alarm_time == datetime(2026, 9, 17, 4, tzinfo=UTC)

    runtime.webhook_task.cancel()
    await asyncio.gather(runtime.webhook_task, return_exceptions=True)
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_request_body_read_timeout_fails_before_provider_ack(hass) -> None:
    """Ingress has a finite read budget inside the provider's two-second limit."""
    entry = _entry()
    runtime = _install_runtime(entry)

    with patch(
        "custom_components.ezviz_iot_cn.webhook._async_read_limited",
        side_effect=TimeoutError,
    ):
        response = await async_handle_native_webhook(
            hass,
            f"{WEBHOOK_ID_PREFIX}test",
            _request(_alarm_envelope()),
            entry=entry,
        )

    assert response.status == HTTPStatus.REQUEST_TIMEOUT
    assert runtime.webhook_stats.valid_envelopes == 0
    await runtime.reconciler.async_close()


@pytest.mark.asyncio
async def test_full_queue_still_acks_and_saturates_drop_counter(hass) -> None:
    """Queue pressure is local state and never degrades provider delivery."""
    entry = _entry()
    runtime = _install_runtime(entry)
    runtime.webhook_queue = asyncio.Queue(maxsize=1)
    runtime.webhook_queue.put_nowait(Mock())
    runtime.webhook_stats.queue_drops = MAX_WEBHOOK_STAT_COUNT

    response = await async_handle_native_webhook(
        hass,
        f"{WEBHOOK_ID_PREFIX}test",
        _request(_alarm_envelope()),
        entry=entry,
    )

    _assert_ack(response)
    assert runtime.webhook_stats.valid_envelopes == 1
    assert runtime.webhook_stats.queue_drops == MAX_WEBHOOK_STAT_COUNT
    runtime.client.assert_not_called()
    runtime.webhook_queue.get_nowait()
    runtime.webhook_queue.task_done()
    await runtime.reconciler.async_close()


def test_webhook_limits_are_finite() -> None:
    """Ingress memory, queue, and diagnostic counters remain bounded."""
    assert MAX_WEBHOOK_BODY_BYTES == 1024 * 1024
    assert WEBHOOK_QUEUE_SIZE == 128
    assert MAX_WEBHOOK_STAT_COUNT == 2**31 - 1
