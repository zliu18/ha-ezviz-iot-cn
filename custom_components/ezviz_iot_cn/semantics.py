"""Persisted evidence for event-derived Home Assistant entities."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from .api.models import channel_uid, device_uid

_STATE_VERSION = 1


class ObservedSemantic(StrEnum):
    """Event semantics that justify creating optional entities."""

    ALARM = "alarm"
    MOTION = "motion"
    PERSON = "person"
    ALARM_IMAGE = "alarm_image"
    CALLING = "calling"
    DOORBELL_RING = "doorbell_ring"
    CALLING_IMAGE = "calling_image"


SemanticListener = Callable[[], None]


class ObservedEntityRegistry:
    """Track real event semantics by stable non-reversible channel identity."""

    def __init__(self, hub_id: str) -> None:
        self._hub_id = hub_id
        self._channels: dict[str, set[ObservedSemantic]] = {}
        self._listeners: set[SemanticListener] = set()

    def observe(
        self,
        device_serial: str,
        channel_no: int,
        semantics: Iterable[ObservedSemantic],
    ) -> bool:
        """Record newly demonstrated semantics and notify discovery listeners."""
        key = self._channel_key(device_serial, channel_no)
        current = self._channels.setdefault(key, set())
        before = len(current)
        current.update(semantics)
        if len(current) == before:
            return False
        for listener in tuple(self._listeners):
            listener()
        return True

    def has(
        self,
        device_serial: str,
        channel_no: int,
        semantic: ObservedSemantic,
    ) -> bool:
        """Return whether one physical channel demonstrated a semantic."""
        return semantic in self._channels.get(
            self._channel_key(device_serial, channel_no),
            (),
        )

    def keys_for(
        self,
        device_serial: str,
        channel_no: int,
    ) -> frozenset[ObservedSemantic]:
        """Return immutable observed semantics for one physical channel."""
        return frozenset(
            self._channels.get(self._channel_key(device_serial, channel_no), ())
        )

    def subscribe(self, listener: SemanticListener) -> Callable[[], None]:
        """Subscribe to registry growth and return an idempotent unsubscribe."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def export_state(self) -> dict[str, Any]:
        """Export bounded semantic evidence without raw device identifiers."""
        return {
            "version": _STATE_VERSION,
            "channels": {
                key: sorted(semantic.value for semantic in semantics)
                for key, semantics in sorted(self._channels.items())
                if semantics
            },
        }

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        """Restore only the current strict semantic-state schema."""
        if payload.get("version") != _STATE_VERSION:
            return
        channels = payload.get("channels")
        if not isinstance(channels, Mapping):
            return
        restored: dict[str, set[ObservedSemantic]] = {}
        for raw_key, raw_semantics in channels.items():
            if not isinstance(raw_key, str) or not _valid_uuid(raw_key):
                continue
            if not isinstance(raw_semantics, list):
                continue
            parsed = {
                semantic
                for value in raw_semantics
                if isinstance(value, str)
                for semantic in _parse_semantic(value)
            }
            if parsed:
                restored[raw_key] = parsed
        self._channels = restored

    @property
    def observed_count(self) -> int:
        """Return aggregate semantic count for redacted diagnostics."""
        return sum(len(semantics) for semantics in self._channels.values())

    def _channel_key(self, device_serial: str, channel_no: int) -> str:
        return channel_uid(device_uid(self._hub_id, device_serial), channel_no)


def _valid_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _parse_semantic(value: str) -> tuple[ObservedSemantic, ...]:
    try:
        return (ObservedSemantic(value),)
    except ValueError:
        return ()
