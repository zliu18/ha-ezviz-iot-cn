"""Private bounded persistence for event state and observed semantics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .control_evidence import AlarmControlEvidenceRegistry
from .event import EventReconciler
from .semantics import ObservedEntityRegistry

_STORE_VERSION = 2
_SAVE_DELAY_SECONDS = 30


class EventStateStore:
    """Persist bounded event digests and non-sensitive entity evidence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        reconciler: EventReconciler,
        observed_registry: ObservedEntityRegistry,
        *,
        control_evidence: AlarmControlEvidenceRegistry | None = None,
        store: Store[dict[str, Any]] | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._observed_registry = observed_registry
        self._control_evidence = control_evidence
        self._store = store or Store(
            hass,
            _STORE_VERSION,
            f"{DOMAIN}.{entry_id}.events",
            private=True,
            atomic_writes=True,
        )

    async def async_restore(self) -> None:
        """Restore only valid sections of the current bounded schema."""
        payload = await self._store.async_load()
        if not isinstance(payload, Mapping):
            return
        event_cursor = payload.get("event_cursor")
        if isinstance(event_cursor, Mapping):
            self._reconciler.restore_state(event_cursor)
        observed = payload.get("observed")
        if isinstance(observed, Mapping):
            self._observed_registry.restore_state(observed)
        alarm_controls = payload.get("alarm_controls")
        if self._control_evidence is not None and isinstance(alarm_controls, Mapping):
            self._control_evidence.restore_state(alarm_controls)

    def async_schedule_save(self) -> None:
        """Coalesce frequent event updates into one bounded delayed write."""
        self._store.async_delay_save(
            self._export_state,
            delay=_SAVE_DELAY_SECONDS,
        )

    async def async_flush(self) -> None:
        """Write current state and cancel any pending delayed write."""
        await self._store.async_save(self._export_state())

    async def async_remove(self) -> None:
        """Delete private state when its config entry is removed."""
        await self._store.async_remove()

    def _export_state(self) -> dict[str, Any]:
        payload = {
            "event_cursor": self._reconciler.export_state(),
            "observed": self._observed_registry.export_state(),
        }
        if self._control_evidence is not None:
            payload["alarm_controls"] = self._control_evidence.export_state()
        return payload
