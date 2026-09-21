"""Reconcile EZVIZ alarm and calling events from supported transports."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from .alarm_semantics import classify_alarm_type
from .api.models import CallingAction, EventSource, EzvizAlarmEvent, EzvizCallingEvent
from .semantics import ObservedEntityRegistry, ObservedSemantic

_STATE_VERSION = 2

ChannelKey = tuple[str, int]
ResetKey = tuple[str, int, str]
NowCallable = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ReconciledAlarmState:
    """Latest semantic alarm state for one device channel."""

    device_serial: str
    channel_no: int
    last_alarm_id: str | None
    last_alarm_type: str
    last_alarm_time: datetime
    latest_alarm_image_url: str | None
    source: EventSource
    motion: bool
    person: bool


@dataclass(frozen=True, slots=True)
class ReconciledCallingState:
    """Latest doorbell calling state for one device channel."""

    device_serial: str
    channel_no: int
    last_calling_id: str | None
    last_calling_time: datetime
    action: CallingAction
    latest_calling_image_url: str | None
    source: EventSource
    ringing: bool


type ReconciledEventState = ReconciledAlarmState | ReconciledCallingState
StateListener = Callable[[ReconciledEventState], None]


class EventReconciler:
    """Merge event transports into bounded, monotonic, transient state."""

    def __init__(
        self,
        observed_registry: ObservedEntityRegistry,
        *,
        now: NowCallable | None = None,
        hold_seconds: float = 30.0,
        max_recent: int = 512,
        retention: timedelta = timedelta(hours=24),
    ) -> None:
        if hold_seconds <= 0:
            raise ValueError("hold_seconds must be positive")
        if max_recent < 1:
            raise ValueError("max_recent must be at least 1")
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self._observed_registry = observed_registry
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._hold_seconds = hold_seconds
        self._max_recent = max_recent
        self._retention = retention
        self._recent: OrderedDict[str, datetime] = OrderedDict()
        self._alarm_states: dict[ChannelKey, ReconciledAlarmState] = {}
        self._calling_states: dict[ChannelKey, ReconciledCallingState] = {}
        self._reset_tasks: dict[ResetKey, asyncio.Task[None]] = {}
        self._listeners: set[StateListener] = set()
        self._cursor: datetime | None = None
        self._closed = False

    @property
    def recent_count(self) -> int:
        """Return the bounded number of retained event digests."""
        return len(self._recent)

    @property
    def active_reset_count(self) -> int:
        """Return active transient reset task count for diagnostics."""
        return sum(not task.done() for task in self._reset_tasks.values())

    def alarm_state_for(
        self,
        device_serial: str,
        channel_no: int,
    ) -> ReconciledAlarmState | None:
        """Return the latest alarm state for one channel."""
        return self._alarm_states.get((device_serial, channel_no))

    def calling_state_for(
        self,
        device_serial: str,
        channel_no: int,
    ) -> ReconciledCallingState | None:
        """Return the latest calling state for one channel."""
        return self._calling_states.get((device_serial, channel_no))

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Subscribe to accepted event-state changes."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def async_process_alarm(self, event: EzvizAlarmEvent) -> bool:
        """Process one normalized alarm and report visible state change."""
        event_time = self._accept_event(event.event_key, event.occurred_at)
        if event_time is None:
            return False
        key = (event.device_serial, event.channel_no)
        previous = self._alarm_states.get(key)
        if previous is not None and event_time < previous.last_alarm_time:
            return False

        classification = classify_alarm_type(event.alarm_type)
        state = ReconciledAlarmState(
            device_serial=event.device_serial,
            channel_no=event.channel_no,
            last_alarm_id=event.alarm_id,
            last_alarm_type=event.alarm_type,
            last_alarm_time=event_time,
            latest_alarm_image_url=event.image_url,
            source=event.source,
            motion=(previous.motion if previous else False) or classification.motion,
            person=(previous.person if previous else False) or classification.person,
        )
        self._alarm_states[key] = state
        semantics = {ObservedSemantic.ALARM}
        if classification.motion:
            semantics.add(ObservedSemantic.MOTION)
        if classification.person:
            semantics.add(ObservedSemantic.PERSON)
        if event.image_url is not None:
            semantics.add(ObservedSemantic.ALARM_IMAGE)
        self._observed_registry.observe(
            event.device_serial,
            event.channel_no,
            semantics,
        )
        self._notify(state)
        if classification.motion:
            self._schedule_reset(key, "motion")
        if classification.person:
            self._schedule_reset(key, "person")
        return True

    async def async_process_call(self, event: EzvizCallingEvent) -> bool:
        """Process one normalized calling lifecycle event."""
        event_time = self._accept_event(event.event_key, event.occurred_at)
        if event_time is None:
            return False
        key = (event.device_serial, event.channel_no)
        previous = self._calling_states.get(key)
        if previous is not None and event_time < previous.last_calling_time:
            return False

        ringing = event.action is CallingAction.RING
        if not ringing:
            self._cancel_reset((*key, "ringing"))
        state = ReconciledCallingState(
            device_serial=event.device_serial,
            channel_no=event.channel_no,
            last_calling_id=event.calling_id,
            last_calling_time=event_time,
            action=event.action,
            latest_calling_image_url=event.image_url,
            source=event.source,
            ringing=ringing,
        )
        self._calling_states[key] = state
        semantics = {ObservedSemantic.CALLING}
        if ringing:
            semantics.add(ObservedSemantic.DOORBELL_RING)
        if event.image_url is not None:
            semantics.add(ObservedSemantic.CALLING_IMAGE)
        self._observed_registry.observe(
            event.device_serial,
            event.channel_no,
            semantics,
        )
        self._notify(state)
        if ringing:
            self._schedule_reset(key, "ringing")
        return True

    def export_state(self) -> dict[str, Any]:
        """Export bounded replay protection without event identifiers."""
        return {
            "version": _STATE_VERSION,
            "cursor": self._cursor.isoformat() if self._cursor else None,
            "recent": [
                {"key": digest, "timestamp": timestamp.isoformat()}
                for digest, timestamp in self._recent.items()
            ],
        }

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        """Restore only the current bounded event-cursor schema."""
        if self._closed or payload.get("version") != _STATE_VERSION:
            return
        now = self._normalize_time(self._now())
        cursor = self._parse_datetime(payload.get("cursor"))
        recent = payload.get("recent")
        if isinstance(recent, list):
            for item in recent:
                if not isinstance(item, Mapping):
                    continue
                digest = item.get("key")
                timestamp = self._parse_datetime(item.get("timestamp"))
                if (
                    isinstance(digest, str)
                    and len(digest) == 64
                    and timestamp is not None
                    and timestamp >= now - self._retention
                ):
                    self._recent[digest] = timestamp
        self._trim_recent()
        if cursor is not None:
            self._cursor = cursor

    async def async_close(self) -> None:
        """Cancel all owned timers and reject future events."""
        if self._closed and not self._reset_tasks:
            return
        self._closed = True
        tasks = tuple(self._reset_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reset_tasks.clear()
        self._listeners.clear()

    def _accept_event(self, event_key: str, occurred_at: datetime) -> datetime | None:
        if self._closed:
            raise RuntimeError("Event reconciler is closed")
        now = self._normalize_time(self._now())
        event_time = self._normalize_time(occurred_at)
        self._prune_recent(now)
        if event_time < now - self._retention:
            return None
        digest = sha256(event_key.encode()).hexdigest()
        if digest in self._recent:
            return None
        self._recent[digest] = event_time
        self._recent.move_to_end(digest)
        self._trim_recent()
        if self._cursor is None or event_time > self._cursor:
            self._cursor = event_time
        return event_time

    def _schedule_reset(self, key: ChannelKey, kind: str) -> None:
        reset_key = (*key, kind)
        self._cancel_reset(reset_key)
        self._reset_tasks[reset_key] = asyncio.create_task(
            self._async_reset_after_hold(key, kind),
            name=f"ezviz-iot-cn-reset-{kind}",
        )

    def _cancel_reset(self, reset_key: ResetKey) -> None:
        previous = self._reset_tasks.pop(reset_key, None)
        if previous is not None:
            previous.cancel()

    async def _async_reset_after_hold(self, key: ChannelKey, kind: str) -> None:
        reset_key = (*key, kind)
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self._hold_seconds)
            if kind in {"motion", "person"}:
                alarm_state = self._alarm_states.get(key)
                if alarm_state is None:
                    return
                if kind == "motion" and alarm_state.motion:
                    updated_alarm_state = replace(
                        alarm_state,
                        motion=False,
                    )
                elif kind == "person" and alarm_state.person:
                    updated_alarm_state = replace(alarm_state, person=False)
                else:
                    return
                self._alarm_states[key] = updated_alarm_state
                updated_state: ReconciledEventState = updated_alarm_state
            else:
                calling_state = self._calling_states.get(key)
                if calling_state is None or not calling_state.ringing:
                    return
                updated_calling_state = replace(calling_state, ringing=False)
                self._calling_states[key] = updated_calling_state
                updated_state = updated_calling_state
            self._notify(updated_state)
        finally:
            if self._reset_tasks.get(reset_key) is task:
                self._reset_tasks.pop(reset_key, None)

    def _notify(self, state: ReconciledEventState) -> None:
        for listener in tuple(self._listeners):
            listener(state)

    def _prune_recent(self, now: datetime) -> None:
        cutoff = now - self._retention
        expired = [
            digest for digest, timestamp in self._recent.items() if timestamp < cutoff
        ]
        for digest in expired:
            self._recent.pop(digest, None)

    def _trim_recent(self) -> None:
        while len(self._recent) > self._max_recent:
            self._recent.popitem(last=False)

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _parse_datetime(cls, value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return cls._normalize_time(datetime.fromisoformat(value))
        except ValueError:
            return None
