"""Async client for the official EZVIZ China Open Platform."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .errors import (
    EzvizApiResponseError,
    EzvizAuthError,
    EzvizConnectionError,
    EzvizNotSupportedError,
    EzvizProtocolError,
    EzvizRateLimitError,
)
from .image_downloader import EzvizImageDownloader
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
    PtzDirection,
)
from .parser import (
    parse_alarm_light_state,
    parse_alarm_page,
    parse_capabilities,
    parse_capture,
    parse_channel_page,
    parse_device_page,
    parse_device_status_alarm_sound_mode,
    parse_night_vision,
    parse_token,
)
from .usage import ApiUsageSnapshot, ApiUsageTracker

BASE_URL = URL("https://open.ys7.com/api/lapp")
NIGHT_VISION_URL = URL("https://open.ys7.com/api/v3/keyValue")
# Official CN authentication guide: https://open.ys7.com/doc/zh/pc/page2.html
TOKEN_URL = BASE_URL / "token/get"
# Official device data reference: https://open.ys7.com/doc/zh/pc/group__data.html
DEVICE_LIST_URL = BASE_URL / "device/list"
# Official device status reference: https://open.ys7.com/doc/zh/pc/group__data.html
DEVICE_STATUS_URL = BASE_URL / "device/status/get"
# Official channel data reference: https://open.ys7.com/doc/zh/pc/group__data.html
CAMERA_LIST_URL = BASE_URL / "camera/list"
# Official device capability reference: https://open.ys7.com/doc/zh/pc/group__device.html
CAPACITY_URL = BASE_URL / "device/capacity"
# Official alarm-list reference: https://open.ys7.com/help/986
ALARM_LIST_URL = BASE_URL / "alarm/list"
# Official capture reference: https://open.ys7.com/doc/zh/pc/group__device.html
CAPTURE_URL = BASE_URL / "device/capture"
# Official defence reference: https://open.ys7.com/doc/zh/pc/group__device.html
DEFENCE_SET_URL = BASE_URL / "device/defence/set"
# Official alarm-sound reference: https://open.ys7.com/doc/zh/pc/group__device.html
ALARM_SOUND_SET_URL = BASE_URL / "device/alarm/sound/set"
# Official switch status reference: https://open.ys7.com/help/1741
ALARM_LIGHT_GET_URL = URL("https://open.ys7.com/api/v3/device/switchStatus/get")
# Official switch control reference: https://open.ys7.com/help/1740
ALARM_LIGHT_SET_URL = URL("https://open.ys7.com/api/v3/device/switchStatus/set")
# Official PTZ reference: https://open.ys7.com/doc/zh/pc/group__device.html
PTZ_START_URL = BASE_URL / "device/ptz/start"
# Official PTZ reference: https://open.ys7.com/doc/zh/pc/group__device.html
PTZ_STOP_URL = BASE_URL / "device/ptz/stop"

_AUTH_ERROR_CODES = frozenset({"10002", "10005"})
_TOKEN_EXPIRED_CODES = frozenset({"10002"})
_RATE_LIMIT_CODES = frozenset({"10007", "10029", "20034"})
_NOT_SUPPORTED_CODES = frozenset({"20014", "60020"})
_MAX_READ_ATTEMPTS = 3
_NIGHT_VISION_READBACK_DELAYS = (0, 1, 2, 4, 7)
_MAX_PAGES = 1000
_TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
_MAX_ALARM_RANGE = timedelta(days=7)
JsonObject = Mapping[str, Any]
SleepCallable = Callable[[float], Awaitable[None]]
NowCallable = Callable[[], datetime]
JitterCallable = Callable[[float, float], float]


class EzvizApiClient:
    """Bounded async API client using a caller-owned aiohttp session."""

    def __init__(
        self,
        session: ClientSession,
        *,
        app_key: str,
        app_secret: str,
        timeout: float = 10.0,
        now: NowCallable | None = None,
        sleep: SleepCallable = asyncio.sleep,
        jitter: JitterCallable = random.uniform,
        image_downloader: EzvizImageDownloader | None = None,
        usage_tracker: ApiUsageTracker | None = None,
    ) -> None:
        self._session = session
        self._app_key = app_key
        self._app_secret = app_secret
        self._timeout = ClientTimeout(total=timeout)
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._sleep = sleep
        self._jitter = jitter
        self._image_downloader = image_downloader
        self._usage_tracker = usage_tracker or ApiUsageTracker(now=self._now)
        self._token: AccessToken | None = None
        self._token_lock = asyncio.Lock()
        self._night_vision_lock = asyncio.Lock()

    @property
    def usage_snapshot(self) -> ApiUsageSnapshot:
        """Return local observations, not an official quota balance."""
        return self._usage_tracker.snapshot

    @property
    def usage_tracker(self) -> ApiUsageTracker:
        """Expose local request observations for hub diagnostics."""
        return self._usage_tracker

    async def async_validate_credentials(
        self,
        *,
        include_shared_devices: bool = False,
    ) -> tuple[EzvizDevice, ...]:
        """Validate credentials and return the accessible device snapshot."""
        await self._async_access_token()
        return await self.async_list_devices(
            include_shared_devices=include_shared_devices
        )

    async def async_list_devices(
        self,
        *,
        page_size: int = 50,
        include_shared_devices: bool = False,
        channels: tuple[EzvizChannel, ...] | None = None,
    ) -> tuple[EzvizDevice, ...]:
        """Return every accessible device using guarded pagination."""
        devices: tuple[EzvizDevice, ...] = await self._async_paginated(
            DEVICE_LIST_URL,
            parse_device_page,
            page_size=page_size,
        )
        if not include_shared_devices:
            return devices
        if channels is None:
            channels = await self.async_list_channels(
                page_size=page_size,
                include_shared_devices=True,
            )
        return _merge_shared_devices(devices, channels)

    async def async_list_channels(
        self,
        *,
        page_size: int = 50,
        include_shared_devices: bool = True,
    ) -> tuple[EzvizChannel, ...]:
        """Return every accessible channel using guarded pagination."""
        channels: tuple[EzvizChannel, ...] = await self._async_paginated(
            CAMERA_LIST_URL,
            parse_channel_page,
            page_size=page_size,
        )
        if include_shared_devices:
            return channels
        return tuple(channel for channel in channels if not channel.is_shared)

    async def async_list_alarms(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        page_size: int = 50,
    ) -> tuple[EzvizAlarmEvent, ...]:
        """Return account-level alarms for local per-device distribution."""
        if page_size < 1 or page_size > 50:
            raise ValueError("page_size must be between 1 and 50")
        if start_time.utcoffset() is None or end_time.utcoffset() is None:
            raise ValueError("alarm query times must be timezone-aware")
        if start_time > end_time:
            raise ValueError("start_time must be before or equal to end_time")
        if end_time - start_time > _MAX_ALARM_RANGE:
            raise ValueError("alarm query range cannot exceed seven days")

        def parser(payload: JsonObject) -> EzvizPage[EzvizAlarmEvent]:
            return parse_alarm_page(payload, source=EventSource.POLL)

        return await self._async_paginated(
            ALARM_LIST_URL,
            parser,
            page_size=page_size,
            extra_form={
                "startTime": int(start_time.timestamp() * 1000),
                "endTime": int(end_time.timestamp() * 1000),
                "alarmType": -1,
                "status": 2,
            },
        )

    async def async_get_capabilities(
        self,
        device_serial: str,
    ) -> EzvizCapabilities:
        """Return the documented capability flags for one device."""
        payload = await self._async_request_json(
            CAPACITY_URL,
            {"deviceSerial": device_serial},
            retryable=True,
        )
        return parse_capabilities(payload, device_serial=device_serial)

    async def async_get_alarm_sound_mode(
        self,
        device_serial: str,
    ) -> AlarmSoundMode | None:
        """Return the writable alarm-sound mode reported by device status."""
        payload = await self._async_request_json(
            DEVICE_STATUS_URL,
            {"deviceSerial": device_serial, "channel": 1},
            retryable=True,
        )
        return parse_device_status_alarm_sound_mode(payload)

    async def async_get_alarm_light_state(
        self,
        device_serial: str,
        *,
        channel_no: int,
        protocol: AlarmLightProtocol,
    ) -> EzvizAlarmLightState:
        """Return one exact documented alarm-light switch state."""
        payload = await self._async_v3_request_json(
            "GET",
            ALARM_LIGHT_GET_URL,
            device_serial=device_serial,
            headers={"channelNo": str(channel_no)},
            params={"type": protocol.value},
            retryable=True,
        )
        return parse_alarm_light_state(
            payload,
            requested_protocol=protocol,
        )

    async def _async_night_vision_request(
        self, serial: str, *, value: str | None = None
    ) -> JsonObject:
        url = NIGHT_VISION_URL / serial / "1" / "op"
        fields = {
            "accessToken": await self._async_access_token(),
            "key": "NightVision_Model",
        }
        if value is None:
            payload = await self._async_http_json(
                "GET", url, headers={}, params=fields, retryable=False
            )
        else:
            payload = await self._async_http_json(
                "PUT",
                url,
                headers={},
                params={},
                form={**fields, "value": value},
                retryable=False,
            )
        meta = payload.get("meta")
        if not isinstance(meta, Mapping):
            raise EzvizProtocolError("Night-vision response metadata missing")
        self._raise_for_application_code(meta)
        return payload

    async def async_get_night_vision(self, serial: str) -> NightVisionSettings:
        """Read one C3C device's current night-vision parameters."""
        async with self._night_vision_lock:
            return parse_night_vision(await self._async_night_vision_request(serial))

    async def async_update_night_vision(
        self,
        serial: str,
        *,
        mode: NightVisionMode | None = None,
        duration: int | None = None,
        brightness: int | None = None,
    ) -> NightVisionSettings:
        """Preserve other parameters and verify the camera's readback."""
        async with self._night_vision_lock:
            before = parse_night_vision(await self._async_night_vision_request(serial))
            wanted = replace(
                before,
                mode=before.mode if mode is None else mode,
                duration=before.duration if duration is None else duration,
                brightness=before.brightness if brightness is None else brightness,
            )
            if wanted == before:
                return before
            if (
                not isinstance(wanted.mode, NightVisionMode)
                or not 0 <= wanted.duration <= 3600
                or not 0 <= wanted.brightness <= 100
            ):
                raise ValueError("Invalid night-vision settings")
            payload = await self._async_night_vision_request(
                serial,
                value=json.dumps(
                    {
                        "graphicType": wanted.mode.value,
                        "duration": wanted.duration,
                        "luminance": wanted.brightness,
                    }
                ),
            )
            data = payload.get("data")
            if not isinstance(data, Mapping) or data.get("result") != "OK":
                raise EzvizProtocolError("Night-vision write was not confirmed")
            # The API acknowledges dispatch before this firmware applies the change.
            # Retry only reads and never repeat an acknowledged write.
            for delay in _NIGHT_VISION_READBACK_DELAYS:
                if delay:
                    await self._sleep(delay)
                try:
                    observed = parse_night_vision(
                        await self._async_night_vision_request(serial)
                    )
                except EzvizConnectionError:
                    continue
                if observed == wanted:
                    return observed
            raise EzvizProtocolError("Night-vision readback did not match")

    async def async_capture(
        self,
        device_serial: str,
        channel_no: int,
    ) -> EzvizCapture:
        """Request a new still capture from one channel."""
        payload = await self._async_request_json(
            CAPTURE_URL,
            {"deviceSerial": device_serial, "channelNo": channel_no},
            retryable=False,
        )
        return parse_capture(
            payload,
            device_serial=device_serial,
            channel_no=channel_no,
        )

    async def async_download_image(self, image_url: str) -> tuple[bytes, str]:
        """Delegate temporary images to the protected dedicated session."""
        if self._image_downloader is None:
            raise EzvizProtocolError("Image downloader is not configured")
        return await self._image_downloader.async_download(image_url)

    async def async_close(self) -> None:
        """Close resources owned by this client without closing the API session."""
        if self._image_downloader is not None:
            await self._image_downloader.async_close()

    async def async_set_defence(
        self,
        device_serial: str,
        *,
        enabled: bool,
    ) -> None:
        """Set cloud alarm arming without automatic write retries."""
        await self._async_control(
            DEFENCE_SET_URL,
            {"deviceSerial": device_serial, "isDefence": int(enabled)},
        )

    async def async_set_alarm_sound(
        self,
        device_serial: str,
        mode: AlarmSoundMode,
    ) -> None:
        """Set the documented device alarm sound mode."""
        await self._async_control(
            ALARM_SOUND_SET_URL,
            {"deviceSerial": device_serial, "type": mode.value},
        )

    async def async_set_alarm_light(
        self,
        device_serial: str,
        *,
        protocol: AlarmLightProtocol,
        enabled: bool,
    ) -> None:
        """Set one verified documented alarm-light switch protocol."""
        await self._async_v3_request_json(
            "POST",
            ALARM_LIGHT_SET_URL,
            device_serial=device_serial,
            params={"enable": int(enabled), "type": protocol.value},
            retryable=False,
        )

    async def async_ptz_start(
        self,
        device_serial: str,
        *,
        channel_no: int,
        direction: PtzDirection,
        speed: int,
    ) -> None:
        """Start a PTZ movement."""
        if speed not in (0, 1, 2):
            raise ValueError("PTZ speed must be 0, 1, or 2")
        await self._async_control(
            PTZ_START_URL,
            {
                "deviceSerial": device_serial,
                "channelNo": channel_no,
                "direction": direction.value,
                "speed": speed,
            },
        )

    async def async_ptz_stop(
        self,
        device_serial: str,
        *,
        channel_no: int,
        direction: PtzDirection,
    ) -> None:
        """Stop a PTZ movement."""
        await self._async_control(
            PTZ_STOP_URL,
            {
                "deviceSerial": device_serial,
                "channelNo": channel_no,
                "direction": direction.value,
            },
        )

    async def _async_control(
        self,
        url: URL,
        form: dict[str, str | int],
    ) -> None:
        payload = await self._async_request_json(url, form, retryable=False)
        self._raise_for_application_code(payload)

    async def _async_paginated[T](
        self,
        url: URL,
        parser: Callable[[JsonObject], Any],
        *,
        page_size: int,
        extra_form: Mapping[str, str | int] | None = None,
    ) -> tuple[T, ...]:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        items: list[T] = []
        page_start = 0
        for _page_guard in range(_MAX_PAGES):
            form: dict[str, str | int] = {
                "pageStart": page_start,
                "pageSize": page_size,
            }
            if extra_form:
                form.update(extra_form)
            payload = await self._async_request_json(url, form, retryable=True)
            page = parser(payload)
            items.extend(page.items)
            if len(items) >= page.total or not page.items:
                return tuple(items)
            next_page = page.page + 1
            if next_page <= page_start:
                raise EzvizProtocolError("Pagination did not advance")
            page_start = next_page
        raise EzvizProtocolError("Pagination exceeded the safety limit")

    async def _async_access_token(self) -> str:
        token = self._token
        if token is not None and token.expires_at - self._now() > _TOKEN_REFRESH_MARGIN:
            return token.token
        async with self._token_lock:
            token = self._token
            if (
                token is not None
                and token.expires_at - self._now() > _TOKEN_REFRESH_MARGIN
            ):
                return token.token
            payload = await self._async_post_json(
                TOKEN_URL,
                {"appKey": self._app_key, "appSecret": self._app_secret},
                retryable=False,
            )
            self._raise_for_application_code(payload)
            token = parse_token(payload)
            self._token = token
            return token.token

    async def _async_invalidate_token(self, rejected_token: str) -> None:
        async with self._token_lock:
            if self._token is not None and self._token.token == rejected_token:
                self._token = None

    async def _async_request_json(
        self,
        url: URL,
        form: Mapping[str, str | int],
        *,
        retryable: bool,
        allow_token_replay: bool = True,
    ) -> JsonObject:
        token = await self._async_access_token()
        authenticated_form = {"accessToken": token, **form}
        payload = await self._async_post_json(
            url,
            authenticated_form,
            retryable=retryable,
        )
        code = str(payload.get("code", ""))
        if code in _TOKEN_EXPIRED_CODES and allow_token_replay:
            await self._async_invalidate_token(token)
            return await self._async_request_json(
                url,
                form,
                retryable=retryable,
                allow_token_replay=False,
            )
        self._raise_for_application_code(payload)
        return payload

    async def _async_v3_request_json(
        self,
        method: str,
        url: URL,
        *,
        device_serial: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        retryable: bool,
        allow_token_replay: bool = True,
    ) -> JsonObject:
        token = await self._async_access_token()
        request_headers = {
            "accessToken": token,
            "deviceSerial": device_serial,
            **(headers or {}),
        }
        payload = await self._async_http_json(
            method,
            url,
            headers=request_headers,
            params=params or {},
            retryable=retryable,
        )
        code = str(payload.get("code", ""))
        if code in _TOKEN_EXPIRED_CODES and allow_token_replay:
            await self._async_invalidate_token(token)
            return await self._async_v3_request_json(
                method,
                url,
                device_serial=device_serial,
                headers=headers,
                params=params,
                retryable=retryable,
                allow_token_replay=False,
            )
        self._raise_for_application_code(payload)
        return payload

    async def _async_post_json(
        self,
        url: URL,
        form: Mapping[str, str | int],
        *,
        retryable: bool,
    ) -> JsonObject:
        attempts = _MAX_READ_ATTEMPTS if retryable else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                self._usage_tracker.record_request()
                async with self._session.post(
                    url,
                    data=dict(form),
                    timeout=self._timeout,
                ) as response:
                    if response.status == 429:
                        self._usage_tracker.record_rate_limit()
                        if attempt + 1 < attempts:
                            await self._async_backoff(attempt, response.headers)
                            continue
                        raise EzvizRateLimitError("EZVIZ API rate limit reached")
                    if response.status >= 500:
                        if attempt + 1 < attempts:
                            await self._async_backoff(attempt, response.headers)
                            continue
                        raise EzvizConnectionError("EZVIZ API server error")
                    if response.status >= 400:
                        raise EzvizConnectionError("EZVIZ API HTTP error")
                    try:
                        payload = await response.json(content_type=None)
                    except (TypeError, ValueError) as err:
                        raise EzvizProtocolError(
                            "EZVIZ API returned invalid JSON"
                        ) from err
                    if not isinstance(payload, Mapping):
                        raise EzvizProtocolError("EZVIZ API response must be an object")
                    return payload
            except (TimeoutError, ClientError) as err:
                last_error = err
                if attempt + 1 < attempts:
                    await self._async_backoff(attempt, {})
                    continue
                raise EzvizConnectionError("Unable to reach EZVIZ API") from err
        raise EzvizConnectionError("Unable to reach EZVIZ API") from last_error

    async def _async_http_json(
        self,
        method: str,
        url: URL,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        retryable: bool,
        form: Mapping[str, str] | None = None,
    ) -> JsonObject:
        attempts = _MAX_READ_ATTEMPTS if retryable else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                self._usage_tracker.record_request()
                if method == "GET":
                    request_context = self._session.get(
                        url,
                        headers=dict(headers),
                        params=dict(params),
                        timeout=self._timeout,
                        allow_redirects=False,
                    )
                elif method == "PUT":
                    request_context = self._session.put(
                        url,
                        headers=dict(headers),
                        params=dict(params),
                        data=form,
                        timeout=self._timeout,
                        allow_redirects=False,
                    )
                else:
                    request_context = self._session.post(
                        url,
                        headers=dict(headers),
                        params=dict(params),
                        timeout=self._timeout,
                        allow_redirects=False,
                    )
                async with request_context as response:
                    if response.status == 429:
                        self._usage_tracker.record_rate_limit()
                        if attempt + 1 < attempts:
                            await self._async_backoff(attempt, response.headers)
                            continue
                        raise EzvizRateLimitError("EZVIZ API rate limit reached")
                    if response.status >= 500:
                        if attempt + 1 < attempts:
                            await self._async_backoff(attempt, response.headers)
                            continue
                        raise EzvizConnectionError("EZVIZ API server error")
                    if response.status >= 400:
                        raise EzvizConnectionError("EZVIZ API HTTP error")
                    try:
                        payload = await response.json(content_type=None)
                    except (TypeError, ValueError) as err:
                        raise EzvizProtocolError(
                            "EZVIZ API returned invalid JSON"
                        ) from err
                    if not isinstance(payload, Mapping):
                        raise EzvizProtocolError("EZVIZ API response must be an object")
                    return payload
            except (TimeoutError, ClientError) as err:
                last_error = err
                if attempt + 1 < attempts:
                    await self._async_backoff(attempt, {})
                    continue
                raise EzvizConnectionError("Unable to reach EZVIZ API") from err
        raise EzvizConnectionError("Unable to reach EZVIZ API") from last_error

    async def _async_backoff(
        self,
        attempt: int,
        headers: Mapping[str, str],
    ) -> None:
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = min(max(float(retry_after), 0.0), 30.0)
            except ValueError:
                delay = 0.5 * (2**attempt)
        else:
            delay = 0.5 * (2**attempt)
        delay += self._jitter(0.0, 0.25)
        await self._sleep(delay)

    def _raise_for_application_code(self, payload: JsonObject) -> None:
        code_value = payload.get("code")
        if code_value is None:
            raise EzvizProtocolError("Response code is missing")
        code = str(code_value)
        if code == "200":
            return
        if code in _AUTH_ERROR_CODES:
            raise EzvizAuthError("EZVIZ authentication was rejected")
        if code in _RATE_LIMIT_CODES:
            self._usage_tracker.record_rate_limit()
            raise EzvizRateLimitError("EZVIZ API rate limit reached")
        if code in _NOT_SUPPORTED_CODES:
            raise EzvizNotSupportedError("EZVIZ operation is not supported")
        raise EzvizApiResponseError(code)


def _merge_shared_devices(
    devices: tuple[EzvizDevice, ...],
    channels: tuple[EzvizChannel, ...],
) -> tuple[EzvizDevice, ...]:
    """Add camera-list-only shared devices without replacing owned metadata."""
    known_serials = {device.serial for device in devices}
    shared_channels: dict[str, list[EzvizChannel]] = {}
    for channel in channels:
        if channel.is_shared and channel.device_serial not in known_serials:
            shared_channels.setdefault(channel.device_serial, []).append(channel)

    merged = list(devices)
    for serial, device_channels in shared_channels.items():
        first_channel = min(device_channels, key=lambda channel: channel.channel_no)
        merged.append(
            EzvizDevice(
                serial=serial,
                name=first_channel.name,
                model=None,
                online=any(channel.online for channel in device_channels),
                defence=None,
                alarm_sound_mode=None,
                firmware_version=None,
                is_shared=True,
            )
        )
    return tuple(merged)
