"""Home Assistant data coordinators for one EZVIZ Open Platform account."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import EzvizApiClient
from .api.errors import (
    EzvizApiResponseError,
    EzvizAuthError,
    EzvizConnectionError,
    EzvizError,
    EzvizNotSupportedError,
    EzvizProtocolError,
)
from .api.models import (
    AlarmLightProtocol,
    EzvizAlarmEvent,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
)
from .api.scheduler import HubRequestScheduler, RequestPriority
from .const import (
    CONF_ALARM_INTERVAL,
    CONF_METADATA_INTERVAL,
    CONF_STATE_INTERVAL,
    DEFAULT_ALARM_INTERVAL,
    DEFAULT_METADATA_INTERVAL,
    DEFAULT_STATE_INTERVAL,
    MAX_ALARM_INTERVAL,
    MAX_METADATA_INTERVAL,
    MAX_STATE_INTERVAL,
    MIN_ALARM_INTERVAL,
    MIN_METADATA_INTERVAL,
    MIN_STATE_INTERVAL,
)
from .control_evidence import AlarmControlEvidenceRegistry
from .event import EventReconciler
from .filtering import device_is_included, include_shared_devices

_LOGGER = logging.getLogger(__name__)
_ALARM_LOOKBACK = timedelta(hours=24)
_ALARM_OVERLAP = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    """Comparable metadata and capability snapshot for selected devices."""

    devices: tuple[EzvizDevice, ...]
    channels: tuple[EzvizChannel, ...]
    capabilities: tuple[EzvizCapabilities, ...]
    alarm_light_probe_reasons: tuple[str, ...] = ()


def capture_channel_key(channel: EzvizChannel) -> str:
    """Identify a capture channel in the options, independently of its display name."""
    return f"{channel.device_serial}:{channel.channel_no}"


def capture_targets(
    snapshot: MetadataSnapshot, selected: Collection[str] = ()
) -> tuple[EzvizChannel, ...]:
    """Return supported channels, restricted to the selection when configured."""
    capable = {
        capability.device_serial
        for capability in snapshot.capabilities
        if capability.supports_capture
    }
    included = {device.serial for device in snapshot.devices} & capable
    return tuple(
        channel
        for channel in snapshot.channels
        if channel.device_serial in included
        and (not selected or capture_channel_key(channel) in selected)
    )


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Comparable account state snapshot for selected devices."""

    devices: tuple[EzvizDevice, ...]


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    """Comparable result of one alarm polling cycle."""

    events: tuple[EzvizAlarmEvent, ...]
    changed_count: int


def _filter_devices(
    devices: tuple[EzvizDevice, ...],
    entry: ConfigEntry,
) -> tuple[EzvizDevice, ...]:
    return tuple(
        device for device in devices if device_is_included(entry.options, device.serial)
    )


def _bounded_interval(
    entry: ConfigEntry,
    option: str,
    default: int,
    minimum: int,
    maximum: int,
) -> timedelta:
    value = entry.options.get(option, default)
    if isinstance(value, bool):
        value = default
    try:
        seconds = int(value)
    except TypeError, ValueError:
        seconds = default
    return timedelta(seconds=max(minimum, min(maximum, seconds)))


class _EzvizCoordinator[T](DataUpdateCoordinator[T]):
    """Common API error mapping for one config-entry coordinator."""

    async def _async_call_api[R](
        self,
        operation: Callable[[], Awaitable[R]],
        *,
        failure: str,
    ) -> R:
        try:
            return await operation()
        except EzvizAuthError as err:
            raise ConfigEntryAuthFailed("EZVIZ credentials were rejected") from err
        except EzvizError as err:
            raise UpdateFailed(failure) from err


class EzvizMetadataCoordinator(_EzvizCoordinator[MetadataSnapshot]):
    """Refresh slow-changing device, channel, and capability metadata."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EzvizApiClient,
        scheduler: HubRequestScheduler,
        control_evidence: AlarmControlEvidenceRegistry | None = None,
        persist_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="EZVIZ IoT CN metadata",
            update_interval=_bounded_interval(
                entry,
                CONF_METADATA_INTERVAL,
                DEFAULT_METADATA_INTERVAL,
                MIN_METADATA_INTERVAL,
                MAX_METADATA_INTERVAL,
            ),
            always_update=False,
        )
        self._entry = entry
        self._client = client
        self._scheduler = scheduler
        self._control_evidence = control_evidence
        self._persist_callback = persist_callback

    async def _async_collect_capabilities(
        self,
        devices: tuple[EzvizDevice, ...],
    ) -> tuple[EzvizCapabilities, ...]:
        capabilities: list[EzvizCapabilities] = []
        first_connection_error: EzvizConnectionError | None = None
        for device in devices:
            try:
                result = await self._client.async_get_capabilities(device.serial)
            except EzvizNotSupportedError:
                continue
            except EzvizConnectionError as err:
                if first_connection_error is None:
                    first_connection_error = err
                continue
            capabilities.append(result)

        if not capabilities and first_connection_error is not None:
            raise first_connection_error
        return tuple(capabilities)

    async def _async_enrich_alarm_sound_modes(
        self,
        devices: tuple[EzvizDevice, ...],
        capabilities: tuple[EzvizCapabilities, ...],
    ) -> tuple[EzvizDevice, ...]:
        """Add verified alarm-sound state without blocking other metadata."""
        by_serial = {item.device_serial: item for item in capabilities}
        enriched: list[EzvizDevice] = []
        for device in devices:
            capability = by_serial.get(device.serial)
            if (
                device.is_shared
                or capability is None
                or not capability.supports_alarm_sound
            ):
                enriched.append(replace(device, alarm_sound_mode=None))
                continue
            try:
                mode = await self._client.async_get_alarm_sound_mode(device.serial)
            except EzvizNotSupportedError:
                mode = None
            except EzvizConnectionError:
                mode = device.alarm_sound_mode
            enriched.append(replace(device, alarm_sound_mode=mode))
        return tuple(enriched)

    async def _async_enrich_alarm_light_states(
        self,
        devices: tuple[EzvizDevice, ...],
        channels: tuple[EzvizChannel, ...],
        capabilities: tuple[EzvizCapabilities, ...],
    ) -> tuple[tuple[EzvizDevice, ...], tuple[str, ...]]:
        """Probe only capable devices and retain verified protocols on failure."""
        by_serial = {item.device_serial: item for item in capabilities}
        first_channel: dict[str, int] = {}
        for channel in channels:
            first_channel.setdefault(channel.device_serial, channel.channel_no)
        enriched: list[EzvizDevice] = []
        reasons: list[str] = []
        for device in devices:
            saved = (
                self._control_evidence.light_protocol_for(device.serial)
                if self._control_evidence is not None
                else None
            )
            capability = by_serial.get(device.serial)
            channel_no = first_channel.get(device.serial)
            verified = None
            if (
                not device.is_shared
                and capability is not None
                and capability.supports_alarm_light
                and channel_no is not None
                and device.online
            ):
                protocols: tuple[AlarmLightProtocol, ...] = (
                    AlarmLightProtocol.MOTION,
                    AlarmLightProtocol.PIR,
                )
                if saved is not None:
                    protocols = (saved, *(item for item in protocols if item != saved))
                temporarily_unavailable = False
                for protocol in protocols:
                    try:
                        verified = await self._client.async_get_alarm_light_state(
                            device.serial, channel_no=channel_no, protocol=protocol
                        )
                    except (
                        EzvizNotSupportedError,
                        EzvizApiResponseError,
                        EzvizProtocolError,
                    ):
                        continue
                    except EzvizConnectionError:
                        temporarily_unavailable = True
                        break
                    else:
                        break
                if verified is None:
                    reasons.append(
                        "light_temporarily_unavailable"
                        if temporarily_unavailable
                        else "light_capable_but_public_status_unsupported"
                    )
            if verified is not None:
                if (
                    self._control_evidence is not None
                    and self._control_evidence.observe_light_protocol(
                        device.serial, verified.protocol
                    )
                    and self._persist_callback is not None
                ):
                    self._persist_callback()
                saved = verified.protocol
            enriched.append(
                replace(
                    device,
                    alarm_light_enabled=None if verified is None else verified.enabled,
                    alarm_light_protocol=saved,
                )
            )
        return tuple(enriched), tuple(reasons)

    async def _async_enrich_night_vision(
        self,
        devices: tuple[EzvizDevice, ...],
        capabilities: tuple[EzvizCapabilities, ...],
    ) -> tuple[EzvizDevice, ...]:
        supported = {
            capability.device_serial
            for capability in capabilities
            if capability.supports_night_vision
            and capability.supports_smart_night_vision
        }
        enriched = []
        for device in devices:
            if (
                device.model == "CS-C3C-3H2EFRL"
                and device.serial in supported
                and not device.is_shared
                and device.online
            ):
                try:
                    night_vision = await self._client.async_get_night_vision(
                        device.serial
                    )
                except EzvizError as err:
                    _LOGGER.debug(
                        "Unable to read night vision (%s)", type(err).__name__
                    )
                else:
                    device = replace(device, night_vision=night_vision)
            enriched.append(device)
        return tuple(enriched)

    async def _async_update_data(self) -> MetadataSnapshot:
        async def fetch() -> MetadataSnapshot:
            include_shared = include_shared_devices(self._entry.options)
            channels = await self._client.async_list_channels(
                include_shared_devices=include_shared
            )
            devices = _filter_devices(
                await self._client.async_list_devices(
                    include_shared_devices=include_shared,
                    channels=channels,
                ),
                self._entry,
            )
            included_serials = {device.serial for device in devices}
            channels = tuple(
                channel
                for channel in channels
                if channel.device_serial in included_serials
            )
            capabilities = await self._async_collect_capabilities(devices)
            devices = await self._async_enrich_alarm_sound_modes(devices, capabilities)
            devices, reasons = await self._async_enrich_alarm_light_states(
                devices, channels, capabilities
            )
            devices = await self._async_enrich_night_vision(devices, capabilities)
            return MetadataSnapshot(devices, channels, capabilities, reasons)

        async def scheduled() -> MetadataSnapshot:
            return await self._scheduler.async_submit_singleflight(
                "metadata",
                fetch,
                priority=RequestPriority.METADATA,
            )

        return await self._async_call_api(
            scheduled,
            failure="EZVIZ metadata update failed",
        )


class EzvizStateCoordinator(_EzvizCoordinator[StateSnapshot]):
    """Refresh account device state in one paginated account operation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EzvizApiClient,
        scheduler: HubRequestScheduler,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="EZVIZ IoT CN state",
            update_interval=_bounded_interval(
                entry,
                CONF_STATE_INTERVAL,
                DEFAULT_STATE_INTERVAL,
                MIN_STATE_INTERVAL,
                MAX_STATE_INTERVAL,
            ),
            always_update=False,
        )
        self._entry = entry
        self._client = client
        self._scheduler = scheduler

    async def _async_update_data(self) -> StateSnapshot:
        include_shared = include_shared_devices(self._entry.options)

        async def scheduled() -> tuple[EzvizDevice, ...]:
            return await self._scheduler.async_submit_singleflight(
                "state",
                lambda: self._client.async_list_devices(
                    include_shared_devices=include_shared
                ),
                priority=RequestPriority.STATE,
            )

        devices = await self._async_call_api(
            scheduled,
            failure="EZVIZ state update failed",
        )
        return StateSnapshot(_filter_devices(devices, self._entry))


class EzvizAlarmCoordinator(_EzvizCoordinator[AlarmSnapshot]):
    """Poll account alarms and feed the shared Push/poll reconciler."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EzvizApiClient,
        scheduler: HubRequestScheduler,
        reconciler: EventReconciler,
        *,
        persist_callback: Callable[[], None] | None = None,
        included_device_serials: Callable[[], frozenset[str]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="EZVIZ IoT CN alarms",
            update_interval=_bounded_interval(
                entry,
                CONF_ALARM_INTERVAL,
                DEFAULT_ALARM_INTERVAL,
                MIN_ALARM_INTERVAL,
                MAX_ALARM_INTERVAL,
            ),
            always_update=False,
        )
        self._entry = entry
        self._client = client
        self._scheduler = scheduler
        self._reconciler = reconciler
        self._persist_callback = persist_callback
        self._included_device_serials = included_device_serials
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._last_successful_query_end: datetime | None = None

    async def _async_update_data(self) -> AlarmSnapshot:
        end_time = self._now()
        lookback_start = end_time - _ALARM_LOOKBACK
        if self._last_successful_query_end is None:
            start_time = lookback_start
        else:
            overlap_start = self._last_successful_query_end - _ALARM_OVERLAP
            start_time = max(lookback_start, min(overlap_start, end_time))

        async def scheduled() -> tuple[EzvizAlarmEvent, ...]:
            return await self._scheduler.async_submit_singleflight(
                "alarms",
                lambda: self._client.async_list_alarms(
                    start_time=start_time,
                    end_time=end_time,
                ),
                priority=RequestPriority.ALARM,
            )

        events = await self._async_call_api(
            scheduled,
            failure="EZVIZ alarm update failed",
        )
        self._last_successful_query_end = end_time
        events = tuple(
            event
            for event in events
            if device_is_included(self._entry.options, event.device_serial)
        )
        if self._included_device_serials is not None:
            included_serials = self._included_device_serials()
            events = tuple(
                event for event in events if event.device_serial in included_serials
            )
        changed_count = 0
        for event in sorted(events, key=lambda item: item.occurred_at):
            changed_count += await self._reconciler.async_process_alarm(event)
        if changed_count and self._persist_callback is not None:
            self._persist_callback()
        return AlarmSnapshot(events, changed_count)
