"""Tests for the async EZVIZ China Open Platform client."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from yarl import URL

from custom_components.ezviz_iot_cn.api.client import (
    ALARM_LIGHT_GET_URL,
    ALARM_LIGHT_SET_URL,
    ALARM_LIST_URL,
    ALARM_SOUND_SET_URL,
    CAMERA_LIST_URL,
    CAPACITY_URL,
    CAPTURE_URL,
    DEFENCE_SET_URL,
    DEVICE_LIST_URL,
    NIGHT_VISION_URL,
    PTZ_START_URL,
    PTZ_STOP_URL,
    TOKEN_URL,
    EzvizApiClient,
)
from custom_components.ezviz_iot_cn.api.errors import (
    EzvizAuthError,
    EzvizConnectionError,
    EzvizProtocolError,
    EzvizRateLimitError,
)
from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    AlarmSoundMode,
    EventSource,
    NightVisionMode,
    PtzDirection,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
DEVICE_STATUS_URL = URL("https://open.ys7.com/api/lapp/device/status/get")
TOKEN_PAYLOAD = {
    "code": "200",
    "data": {
        "accessToken": "synthetic-token",
        "expireTime": 1789650000000,
    },
}
EMPTY_PAGE = {
    "code": "200",
    "data": [],
    "page": {"total": 0, "page": 0, "size": 50},
}


@dataclass(slots=True)
class _ResponseSpec:
    payload: object = None
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    repeat: bool = False
    callback: Callable[[dict[str, str | int]], object] | None = None


class _FakeResponse:
    def __init__(self, spec: _ResponseSpec, payload: object) -> None:
        self.status = spec.status
        self.headers = spec.headers
        self._payload = payload
        self.content = _FakeContent(payload if isinstance(payload, bytes) else b"")

    async def json(self, *, content_type: object = None) -> object:
        del content_type
        return self._payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback


class _FakeSession:
    def __init__(self) -> None:
        self._responses: dict[URL, deque[_ResponseSpec]] = defaultdict(deque)
        self.calls: list[tuple[URL, dict[str, str | int]]] = []
        self.requests: list[dict[str, object]] = []

    def add(
        self,
        url: URL,
        *,
        payload: object = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        repeat: bool = False,
        callback: Callable[[dict[str, str | int]], object] | None = None,
    ) -> None:
        self._responses[url].append(
            _ResponseSpec(
                payload=payload,
                status=status,
                headers=headers or {},
                repeat=repeat,
                callback=callback,
            )
        )

    def post(
        self,
        url: URL,
        *,
        data: dict[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: object,
        allow_redirects: bool | None = None,
    ) -> _FakeResponse:
        del timeout
        request_url = URL(url)
        queue = self._responses[request_url]
        if not queue:
            raise AssertionError(f"No response registered for POST {request_url}")
        spec = queue[0]
        if not spec.repeat:
            queue.popleft()
        form = dict(data or params or {})
        self.calls.append((request_url, form))
        request: dict[str, object] = {
            "method": "POST",
            "url": request_url,
            "data": dict(data or {}),
            "headers": dict(headers or {}),
            "params": dict(params or {}),
        }
        if allow_redirects is not None:
            request["allow_redirects"] = allow_redirects
        self.requests.append(request)
        payload = spec.callback(form) if spec.callback else spec.payload
        return _FakeResponse(spec, payload)

    def put(self, url: URL, **kwargs) -> _FakeResponse:
        response = self.post(url, **kwargs)
        self.requests[-1]["method"] = "PUT"
        return response

    def get(
        self,
        url: URL,
        *,
        headers: Mapping[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: object,
        allow_redirects: bool = True,
    ) -> _FakeResponse:
        del timeout
        assert allow_redirects is False
        request_url = URL(url)
        queue = self._responses[request_url]
        if not queue:
            raise AssertionError(f"No response registered for GET {request_url}")
        spec = queue[0]
        if not spec.repeat:
            queue.popleft()
        form = dict(params or {})
        self.calls.append((request_url, form))
        self.requests.append(
            {
                "method": "GET",
                "url": request_url,
                "headers": dict(headers or {}),
                "params": form,
            }
        )
        return _FakeResponse(spec, spec.payload)

    def calls_for(self, url: URL) -> list[dict[str, str | int]]:
        return [form for called_url, form in self.calls if called_url == url]


class _FakeContent:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._content
        return self._content[:size]


def _device_page(
    serial: str,
    *,
    page: int,
    total: int,
    size: int = 1,
) -> dict[str, object]:
    return {
        "code": "200",
        "data": [
            {
                "deviceSerial": serial,
                "deviceName": f"Camera {serial[-1]}",
                "deviceType": "CS-SYNTHETIC",
                "status": 1,
                "defence": 1,
                "alarmSoundMode": 2,
                "deviceVersion": "V1.0.0",
            }
        ],
        "page": {"total": total, "page": page, "size": size},
    }


def _shared_channel_page(serial: str) -> dict[str, object]:
    return {
        "code": "200",
        "data": [
            {
                "id": 1001,
                "deviceSerial": serial,
                "channelNo": 1,
                "channelName": "Shared entrance",
                "status": 1,
                "isEncrypt": 1,
                "isShared": 2,
                "permission": "1,2,3",
                "picUrl": "https://example.invalid/shared.jpg",
                "videoLevel": 2,
            }
        ],
        "page": {"total": 1, "page": 0, "size": 50},
    }


def _client(
    session: _FakeSession,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    image_downloader: object | None = None,
) -> EzvizApiClient:
    return EzvizApiClient(
        session,  # type: ignore[arg-type]
        app_key="synthetic-app-key",
        app_secret="synthetic-app-secret",
        now=lambda: NOW,
        sleep=sleep,
        jitter=lambda _low, _high: 0.0,
        image_downloader=image_downloader,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_concurrent_reads_share_one_token_request() -> None:
    """Concurrent coordinator reads must not create a token request storm."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEVICE_LIST_URL, payload=EMPTY_PAGE, repeat=True)
    client = _client(session)

    first, second = await asyncio.gather(
        client.async_list_devices(),
        client.async_list_devices(),
    )

    assert first == second == ()
    assert len(session.calls_for(TOKEN_URL)) == 1


@pytest.mark.asyncio
async def test_expired_token_refreshes_and_replays_safe_read_once() -> None:
    """An application token-expired response permits one synchronized replay."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        TOKEN_URL,
        payload={
            "code": "200",
            "data": {
                "accessToken": "synthetic-token-refreshed",
                "expireTime": 1789653600000,
            },
        },
    )
    session.add(
        DEVICE_LIST_URL,
        payload={"code": "10002", "msg": "token expired", "data": None},
    )
    session.add(DEVICE_LIST_URL, payload=EMPTY_PAGE)

    devices = await _client(session).async_list_devices()

    assert devices == ()
    assert len(session.calls_for(TOKEN_URL)) == 2
    assert len(session.calls_for(DEVICE_LIST_URL)) == 2


@pytest.mark.asyncio
async def test_invalid_credentials_do_not_leak_remote_message_or_credentials() -> None:
    """Auth exceptions must not repeat server text or local credentials."""
    remote_secret = "remote-message-containing-sensitive-data"
    session = _FakeSession()
    session.add(
        TOKEN_URL,
        payload={"code": "10005", "msg": remote_secret, "data": None},
    )

    with pytest.raises(EzvizAuthError) as caught:
        await _client(session).async_validate_credentials()

    message = str(caught.value)
    assert remote_secret not in message
    assert "synthetic-app-key" not in message
    assert "synthetic-app-secret" not in message


@pytest.mark.asyncio
async def test_credential_validation_batches_device_discovery() -> None:
    """Hub setup validates access without requesting one page per device."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        DEVICE_LIST_URL,
        payload=_device_page("SYNTHETIC-SERIAL-001", page=0, total=1),
    )

    devices = await _client(session).async_validate_credentials()

    assert [device.serial for device in devices] == ["SYNTHETIC-SERIAL-001"]
    assert session.calls_for(DEVICE_LIST_URL) == [
        {"accessToken": "synthetic-token", "pageStart": 0, "pageSize": 50}
    ]


@pytest.mark.asyncio
async def test_credential_validation_includes_shared_devices_from_camera_list() -> None:
    """Dropping shared synthesis would reproduce a successful empty Hub setup."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEVICE_LIST_URL, payload=EMPTY_PAGE)
    session.add(
        CAMERA_LIST_URL,
        payload=_shared_channel_page("SYNTHETIC-SHARED-SERIAL-001"),
    )

    devices = await _client(session).async_validate_credentials(
        include_shared_devices=True
    )

    assert len(devices) == 1
    assert devices[0].serial == "SYNTHETIC-SHARED-SERIAL-001"
    assert devices[0].name == "Shared entrance"
    assert devices[0].model is None
    assert devices[0].online is True
    assert devices[0].is_shared is True


@pytest.mark.asyncio
async def test_credential_validation_can_exclude_shared_devices() -> None:
    """Disabling shared discovery must avoid adding camera-list-only devices."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEVICE_LIST_URL, payload=EMPTY_PAGE)

    devices = await _client(session).async_validate_credentials(
        include_shared_devices=False
    )

    assert devices == ()
    assert session.calls_for(CAMERA_LIST_URL) == []


@pytest.mark.asyncio
async def test_direct_device_metadata_wins_over_shared_channel_evidence() -> None:
    """A shared channel flag cannot replace richer direct device metadata."""
    serial = "SYNTHETIC-SERIAL-001"
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        DEVICE_LIST_URL,
        payload=_device_page(serial, page=0, total=1),
    )
    session.add(CAMERA_LIST_URL, payload=_shared_channel_page(serial))

    devices = await _client(session).async_list_devices(include_shared_devices=True)

    assert len(devices) == 1
    assert devices[0].name == "Camera 1"
    assert devices[0].model == "CS-SYNTHETIC"
    assert devices[0].is_shared is False


@pytest.mark.asyncio
async def test_device_list_fetches_every_page() -> None:
    """Device discovery must not silently stop after the first page."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        DEVICE_LIST_URL,
        payload=_device_page("SYNTHETIC-SERIAL-001", page=0, total=2),
    )
    session.add(
        DEVICE_LIST_URL,
        payload=_device_page("SYNTHETIC-SERIAL-002", page=1, total=2),
    )

    devices = await _client(session).async_list_devices(page_size=1)

    assert [device.serial for device in devices] == [
        "SYNTHETIC-SERIAL-001",
        "SYNTHETIC-SERIAL-002",
    ]
    requests = session.calls_for(DEVICE_LIST_URL)
    assert [request["pageStart"] for request in requests] == [0, 1]
    assert all(request["pageSize"] == 1 for request in requests)


@pytest.mark.asyncio
async def test_retryable_read_has_bounded_retries_and_backoff() -> None:
    """Transient read failures retry twice and then succeed."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEVICE_LIST_URL, status=500)
    session.add(DEVICE_LIST_URL, status=503)
    session.add(DEVICE_LIST_URL, payload=EMPTY_PAGE)

    devices = await _client(session, sleep=fake_sleep).async_list_devices()

    assert devices == ()
    assert delays == [0.5, 1.0]
    assert len(session.calls_for(DEVICE_LIST_URL)) == 3


@pytest.mark.asyncio
async def test_rate_limit_stops_after_bounded_retries() -> None:
    """HTTP 429 never creates an unbounded retry loop."""

    async def fake_sleep(_delay: float) -> None:
        return None

    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEVICE_LIST_URL, status=429)
    session.add(DEVICE_LIST_URL, status=429)
    session.add(DEVICE_LIST_URL, status=429)

    with pytest.raises(EzvizRateLimitError):
        await _client(session, sleep=fake_sleep).async_list_devices()

    assert len(session.calls_for(DEVICE_LIST_URL)) == 3


@pytest.mark.asyncio
async def test_control_write_is_not_automatically_retried() -> None:
    """A failed control must not be sent repeatedly by the network layer."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(DEFENCE_SET_URL, status=500, repeat=True)

    with pytest.raises(EzvizConnectionError):
        await _client(session).async_set_defence("SYNTHETIC-SERIAL-001", enabled=True)

    assert len(session.calls_for(DEFENCE_SET_URL)) == 1


@pytest.mark.asyncio
async def test_discovery_alarm_and_capability_methods_use_account_batching() -> None:
    """Channel and alarm discovery use account-level endpoints."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        CAMERA_LIST_URL,
        payload={
            "code": "200",
            "data": [
                {
                    "deviceSerial": "SYNTHETIC-SERIAL-001",
                    "channelNo": 1,
                    "channelName": "Front",
                    "status": 1,
                    "isEncrypt": 0,
                }
            ],
            "page": {"total": 1, "page": 0, "size": 50},
        },
    )
    session.add(
        ALARM_LIST_URL,
        payload={
            "code": "200",
            "data": [],
            "page": {"total": 0, "page": 0, "size": 50},
        },
    )
    session.add(
        CAPACITY_URL,
        payload={
            "code": "200",
            "data": {"support_capture": "1", "support_ptz": "1"},
        },
    )
    client = _client(session)

    channels = await client.async_list_channels()
    alarms = await client.async_list_alarms(
        start_time=NOW - timedelta(hours=24),
        end_time=NOW,
    )
    capabilities = await client.async_get_capabilities("SYNTHETIC-SERIAL-001")

    assert len(channels) == 1
    assert alarms == ()
    assert capabilities.supports_capture is True
    assert capabilities.supports_ptz is True
    alarm_form = session.calls_for(ALARM_LIST_URL)[0]
    assert alarm_form["status"] == 2
    assert alarm_form["alarmType"] == -1
    assert alarm_form["startTime"] == int(
        (NOW - timedelta(hours=24)).timestamp() * 1000
    )
    assert alarm_form["endTime"] == int(NOW.timestamp() * 1000)
    assert alarms == tuple(
        event for event in alarms if event.source is EventSource.POLL
    )


@pytest.mark.asyncio
async def test_device_status_reads_alarm_sound_mode_for_channel_one() -> None:
    """Alarm-sound support comes from the current device-status response."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        DEVICE_STATUS_URL,
        payload={"code": "200", "data": {"alarmSoundMode": 2}},
    )

    mode = await _client(session).async_get_alarm_sound_mode("SYNTHETIC-SERIAL-001")

    assert mode is AlarmSoundMode.SILENT
    assert session.calls_for(DEVICE_STATUS_URL) == [
        {
            "accessToken": "synthetic-token",
            "deviceSerial": "SYNTHETIC-SERIAL-001",
            "channel": 1,
        }
    ]


@pytest.mark.asyncio
async def test_alarm_light_get_and_set_use_verified_v3_protocol() -> None:
    """Alarm-light reads and writes use the same exact documented type."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        ALARM_LIGHT_GET_URL,
        payload={
            "code": "200",
            "data": {"type": 305, "enable": True},
        },
    )
    session.add(ALARM_LIGHT_SET_URL, payload={"code": "200", "msg": "ok"})
    client = _client(session)

    state = await client.async_get_alarm_light_state(
        "SYNTHETIC-SERIAL-001",
        channel_no=1,
        protocol=AlarmLightProtocol.PIR,
    )
    await client.async_set_alarm_light(
        "SYNTHETIC-SERIAL-001",
        protocol=AlarmLightProtocol.PIR,
        enabled=False,
    )

    assert state.protocol is AlarmLightProtocol.PIR
    assert state.enabled is True
    assert session.requests[-2] == {
        "method": "GET",
        "url": ALARM_LIGHT_GET_URL,
        "headers": {
            "accessToken": "synthetic-token",
            "deviceSerial": "SYNTHETIC-SERIAL-001",
            "channelNo": "1",
        },
        "params": {"type": 305},
    }
    assert session.requests[-1] == {
        "method": "POST",
        "url": ALARM_LIGHT_SET_URL,
        "data": {},
        "allow_redirects": False,
        "headers": {
            "accessToken": "synthetic-token",
            "deviceSerial": "SYNTHETIC-SERIAL-001",
        },
        "params": {"enable": 0, "type": 305},
    }
    assert client.usage_snapshot.request_attempts == 3


@pytest.mark.asyncio
async def test_official_quota_code_is_counted_as_rate_limit_not_auth() -> None:
    """Official code 10007 describes exhausted calls, not bad credentials."""
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        DEVICE_STATUS_URL,
        payload={"code": "10007", "msg": "call limit reached"},
    )
    client = _client(session)

    with pytest.raises(EzvizRateLimitError):
        await client.async_get_alarm_sound_mode("SYNTHETIC-SERIAL-001")

    assert client.usage_snapshot.request_attempts == 2
    assert client.usage_snapshot.rate_limit_events == 1
    assert client.usage_snapshot.last_rate_limit_at == NOW


@pytest.mark.asyncio
async def test_alarm_list_rejects_page_sizes_above_official_limit() -> None:
    """The alarm/list endpoint documents a maximum page size of 50."""
    session = _FakeSession()

    with pytest.raises(ValueError, match="between 1 and 50"):
        await _client(session).async_list_alarms(
            start_time=NOW - timedelta(hours=24),
            end_time=NOW,
            page_size=51,
        )

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_time", "end_time", "message"),
    [
        (NOW.replace(tzinfo=None), NOW, "timezone-aware"),
        (NOW, NOW.replace(tzinfo=None), "timezone-aware"),
        (NOW, NOW - timedelta(seconds=1), "before or equal"),
        (NOW - timedelta(days=7, seconds=1), NOW, "seven days"),
    ],
)
async def test_alarm_list_rejects_invalid_query_windows(
    start_time: datetime,
    end_time: datetime,
    message: str,
) -> None:
    """Alarm ranges must follow the documented bounded time contract."""
    session = _FakeSession()

    with pytest.raises(ValueError, match=message):
        await _client(session).async_list_alarms(
            start_time=start_time,
            end_time=end_time,
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_capture_and_control_methods_send_documented_wire_values() -> None:
    """Control methods use numeric API values, not translated display text."""
    seen: dict[str, dict[str, str | int]] = {}

    def capture_form(name: str, payload: object):
        def callback(form: dict[str, str | int]) -> object:
            seen[name] = form
            return payload

        return callback

    success = {"code": "200", "data": None}
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    session.add(
        CAPTURE_URL,
        callback=capture_form(
            "capture",
            {
                "code": "200",
                "data": {"picUrl": "https://example.invalid/capture.jpg"},
            },
        ),
    )
    session.add(DEFENCE_SET_URL, callback=capture_form("defence", success))
    session.add(ALARM_SOUND_SET_URL, callback=capture_form("sound", success))
    session.add(PTZ_START_URL, callback=capture_form("ptz_start", success))
    session.add(PTZ_STOP_URL, callback=capture_form("ptz_stop", success))
    client = _client(session)

    capture = await client.async_capture("SYNTHETIC-SERIAL-001", 1)
    await client.async_set_defence("SYNTHETIC-SERIAL-001", enabled=False)
    await client.async_set_alarm_sound("SYNTHETIC-SERIAL-001", AlarmSoundMode.SILENT)
    await client.async_ptz_start(
        "SYNTHETIC-SERIAL-001",
        channel_no=1,
        direction=PtzDirection.LEFT,
        speed=1,
    )
    await client.async_ptz_stop(
        "SYNTHETIC-SERIAL-001",
        channel_no=1,
        direction=PtzDirection.LEFT,
    )

    assert capture.image_url == "https://example.invalid/capture.jpg"
    common = {
        "accessToken": "synthetic-token",
        "deviceSerial": "SYNTHETIC-SERIAL-001",
    }
    assert seen["capture"] == {**common, "channelNo": 1}
    assert seen["defence"] == {**common, "isDefence": 0}
    assert seen["sound"] == {**common, "type": 2}
    assert seen["ptz_start"] == {
        **common,
        "channelNo": 1,
        "direction": 2,
        "speed": 1,
    }
    assert seen["ptz_stop"] == {
        **common,
        "channelNo": 1,
        "direction": 2,
    }


@pytest.mark.asyncio
async def test_image_download_delegates_without_using_open_api_session() -> None:
    """The Open API client cannot perform a second image-host resolution."""
    session = _FakeSession()
    downloader = AsyncMock()
    downloader.async_download.return_value = (b"synthetic-image", "image/jpeg")
    client = _client(session, image_downloader=downloader)

    result = await client.async_download_image("https://image.example/test.jpg")
    await client.async_close()

    assert result == (b"synthetic-image", "image/jpeg")
    downloader.async_download.assert_awaited_once_with("https://image.example/test.jpg")
    downloader.async_close.assert_awaited_once_with()
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_night_vision_update_preserves_other_parameters_and_checks_readback(
    confirmed: bool,
) -> None:
    session = _FakeSession()
    session.add(TOKEN_URL, payload=TOKEN_PAYLOAD)
    serial = "SYNTHETIC-SERIAL"
    endpoint = NIGHT_VISION_URL / serial / "1" / "op"

    def payload(mode: int) -> dict:
        return {
            "meta": {"code": 200},
            "data": {
                "valueInfo": {
                    "graphicType": mode,
                    "duration": 90,
                    "luminance": 73,
                }
            },
        }

    session.add(endpoint, payload=payload(2))
    session.add(endpoint, payload={"meta": {"code": 200}, "data": {"result": "OK"}})
    session.add(endpoint, payload=payload(2))
    session.add(endpoint, payload=payload(1 if confirmed else 2), repeat=True)
    sleep = AsyncMock()
    client = _client(session, sleep=sleep)
    if confirmed:
        result = await client.async_update_night_vision(
            serial, mode=NightVisionMode.FULL_COLOR
        )
        assert result.mode is NightVisionMode.FULL_COLOR
        assert (result.duration, result.brightness) == (90, 73)
    else:
        with pytest.raises(EzvizProtocolError, match="readback did not match"):
            await client.async_update_night_vision(
                serial, mode=NightVisionMode.FULL_COLOR
            )
    requests = [request for request in session.requests if request["url"] == endpoint]
    expected_reads = 2 if confirmed else 5
    assert [request["method"] for request in requests] == [
        "GET",
        "PUT",
        *(["GET"] * expected_reads),
    ]
    assert [call.args[0] for call in sleep.await_args_list] == (
        [1] if confirmed else [1, 2, 4, 7]
    )
    assert json.loads(session.calls_for(endpoint)[1]["value"]) == {
        "graphicType": 1,
        "duration": 90,
        "luminance": 73,
    }
