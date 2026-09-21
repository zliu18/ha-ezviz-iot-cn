"""Persistent non-sensitive evidence for verified device controls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .api.models import AlarmLightProtocol, device_uid


class AlarmControlEvidenceRegistry:
    """Remember verified alarm-control protocols by non-reversible device ID."""

    def __init__(self, hub_id: str) -> None:
        self._hub_id = hub_id
        self._light_protocols: dict[str, AlarmLightProtocol] = {}

    def light_protocol_for(self, device_serial: str) -> AlarmLightProtocol | None:
        """Return the verified light protocol for one current device."""
        return self._light_protocols.get(device_uid(self._hub_id, device_serial))

    def observe_light_protocol(
        self,
        device_serial: str,
        protocol: AlarmLightProtocol,
    ) -> bool:
        """Record a verified protocol and return whether evidence changed."""
        key = device_uid(self._hub_id, device_serial)
        if self._light_protocols.get(key) is protocol:
            return False
        self._light_protocols[key] = protocol
        return True

    def export_state(self) -> dict[str, dict[str, int]]:
        """Export only non-reversible identifiers and documented values."""
        return {
            "light_protocols": {
                key: protocol.value for key, protocol in self._light_protocols.items()
            }
        }

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        """Restore valid protocol evidence while ignoring corrupt entries."""
        raw_protocols = payload.get("light_protocols")
        if not isinstance(raw_protocols, Mapping):
            return
        restored: dict[str, AlarmLightProtocol] = {}
        for key, raw_protocol in raw_protocols.items():
            if not isinstance(key, str):
                continue
            try:
                identity = UUID(key)
                if identity.version != 5 or str(identity) != key:
                    continue
                restored[key] = AlarmLightProtocol(raw_protocol)
            except TypeError, ValueError:
                continue
        self._light_protocols = restored
