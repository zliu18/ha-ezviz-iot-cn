"""Tests for bounded event deduplication and semantic persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ezviz_iot_cn.api.models import (
    AlarmLightProtocol,
    EventSource,
    EzvizAlarmEvent,
)
from custom_components.ezviz_iot_cn.control_evidence import (
    AlarmControlEvidenceRegistry,
)
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.persistence import EventStateStore
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
HUB_ID = "8ab35d94-a5ec-5c8a-a5bc-48e8e293722b"
SERIAL = "SYNTHETIC-SERIAL-001"


def _event() -> EzvizAlarmEvent:
    return EzvizAlarmEvent(
        alarm_id="alarm-persisted",
        device_serial=SERIAL,
        channel_no=1,
        alarm_type="motion",
        occurred_at=NOW,
        image_url="https://example.invalid/alarm.jpg",
        source=EventSource.POLL,
    )


@pytest.mark.asyncio
async def test_restore_loads_dedupe_and_observed_semantics(hass) -> None:
    """A restart restores replay protection and demonstrated entity support."""
    original_registry = ObservedEntityRegistry(HUB_ID)
    original_reconciler = EventReconciler(original_registry, now=lambda: NOW)
    await original_reconciler.async_process_alarm(_event())
    original_registry.observe(SERIAL, 1, {ObservedSemantic.MOTION})
    payload = {
        "event_cursor": original_reconciler.export_state(),
        "observed": original_registry.export_state(),
    }
    await original_reconciler.async_close()

    store = Mock()
    store.async_load = AsyncMock(return_value=payload)
    restored_registry = ObservedEntityRegistry(HUB_ID)
    restored_reconciler = EventReconciler(restored_registry, now=lambda: NOW)
    persistence = EventStateStore(
        hass,
        "entry-id",
        restored_reconciler,
        restored_registry,
        store=store,
    )

    await persistence.async_restore()

    assert await restored_reconciler.async_process_alarm(_event()) is False
    assert restored_registry.has(SERIAL, 1, ObservedSemantic.MOTION) is True
    store.async_load.assert_awaited_once_with()
    await restored_reconciler.async_close()


@pytest.mark.asyncio
async def test_restore_loads_alarm_light_protocol_without_raw_serial(hass) -> None:
    """A verified light protocol survives restart as non-reversible evidence."""
    original_controls = AlarmControlEvidenceRegistry(HUB_ID)
    original_controls.observe_light_protocol(SERIAL, AlarmLightProtocol.PIR)
    payload = {"alarm_controls": original_controls.export_state()}
    assert SERIAL not in str(payload)
    payload["alarm_controls"]["light_protocols"][SERIAL] = 301

    store = Mock()
    store.async_load = AsyncMock(return_value=payload)
    registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(registry, now=lambda: NOW)
    restored_controls = AlarmControlEvidenceRegistry(HUB_ID)
    persistence = EventStateStore(
        hass,
        "entry-id",
        reconciler,
        registry,
        control_evidence=restored_controls,
        store=store,
    )

    await persistence.async_restore()

    assert restored_controls.light_protocol_for(SERIAL) is AlarmLightProtocol.PIR
    assert SERIAL not in str(restored_controls.export_state())
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_schedule_and_flush_never_persist_sensitive_event_data(hass) -> None:
    """Delayed and final writes contain UUID evidence and digests only."""
    registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(registry, now=lambda: NOW)
    await reconciler.async_process_alarm(_event())
    registry.observe(
        SERIAL,
        1,
        {ObservedSemantic.ALARM, ObservedSemantic.ALARM_IMAGE},
    )
    store = Mock()
    store.async_delay_save = Mock()
    store.async_save = AsyncMock()
    persistence = EventStateStore(
        hass,
        "entry-id",
        reconciler,
        registry,
        store=store,
    )

    persistence.async_schedule_save()

    data_func = store.async_delay_save.call_args.args[0]
    delayed = data_func()
    assert store.async_delay_save.call_args.kwargs == {"delay": 30}
    assert delayed == {
        "event_cursor": reconciler.export_state(),
        "observed": registry.export_state(),
    }
    assert SERIAL not in str(delayed)
    assert "https://" not in str(delayed)
    assert "alarm-persisted" not in str(delayed)

    await persistence.async_flush()

    saved = store.async_save.await_args.args[0]
    assert saved == delayed
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_restore_ignores_corrupt_sections_independently(hass) -> None:
    """One corrupt section cannot block restoration of the other section."""
    registry = ObservedEntityRegistry(HUB_ID)
    registry.observe(SERIAL, 1, {ObservedSemantic.PERSON})
    valid_observed = registry.export_state()

    store = Mock()
    store.async_load = AsyncMock(
        return_value={
            "event_cursor": ["not", "a", "mapping"],
            "observed": valid_observed,
        }
    )
    restored_registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(restored_registry, now=lambda: NOW)
    persistence = EventStateStore(
        hass,
        "entry-id",
        reconciler,
        restored_registry,
        store=store,
    )

    await persistence.async_restore()

    assert reconciler.recent_count == 0
    assert restored_registry.has(SERIAL, 1, ObservedSemantic.PERSON) is True
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_missing_or_non_mapping_store_payload_is_ignored(hass) -> None:
    """A missing current schema cannot prevent the hub from starting."""
    registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(registry, now=lambda: NOW)
    store = Mock()
    store.async_load = AsyncMock(return_value=["not", "a", "mapping"])
    persistence = EventStateStore(
        hass,
        "entry-id",
        reconciler,
        registry,
        store=store,
    )

    await persistence.async_restore()

    assert reconciler.recent_count == 0
    assert registry.observed_count == 0
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_remove_deletes_private_entry_store(hass) -> None:
    """Removing a config entry must not leave orphaned private storage."""
    registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(registry, now=lambda: NOW)
    store = Mock()
    store.async_remove = AsyncMock()
    persistence = EventStateStore(
        hass,
        "entry-id",
        reconciler,
        registry,
        store=store,
    )

    await persistence.async_remove()

    store.async_remove.assert_awaited_once_with()
    await reconciler.async_close()
