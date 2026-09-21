"""Tests for transport-independent alarm and calling reconciliation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ezviz_iot_cn.api.models import (
    CallingAction,
    EventSource,
    EzvizAlarmEvent,
    EzvizCallingEvent,
)
from custom_components.ezviz_iot_cn.event import EventReconciler
from custom_components.ezviz_iot_cn.semantics import (
    ObservedEntityRegistry,
    ObservedSemantic,
)

BASE_TIME = datetime(2026, 9, 18, 12, tzinfo=UTC)
HUB_ID = "8ab35d94-a5ec-5c8a-a5bc-48e8e293722b"
SERIAL = "SYNTHETIC-SERIAL-001"

_OFFICIAL_MOTION_ONLY_ALARM_TYPES = (
    "10002",
    "motionDetect",
    "10013",
    "lineDetection",
    "10014",
    "fieldDetection",
    "10028",
    "runDetection",
    "10029",
    "enterAreaDetection",
    "10030",
    "exitAreaDetection",
    "10033",
    "intrusion",
    "10079",
    "intelligentDetection_d",
    "10122",
    "detectorMotionAlarm",
    "10130",
    "car_detect",
    "10131",
    "human_car_alarm",
    "12000",
    "VMD",
    "12185",
    "lpr",
    "13201",
    "linecrossArm",
    "15003",
    "pet_detection",
    "15006",
    "deviceIntelligentDetection",
    "15010",
    "intelligentDetection",
    "15501",
    "vas_smart_pet",
    "15502",
    "vas_smart_car",
)

_OFFICIAL_PERSON_ALARM_TYPES = (
    "10000",
    "pir",
    "10010",
    "infrared",
    "10015",
    "faceDetection",
    "10026",
    "highDensityDetection",
    "10027",
    "loiterDetection",
    "10035",
    "baby_motion",
    "10042",
    "whitelistFace",
    "10077",
    "face_stranger",
    "10078",
    "face_compare",
    "10088",
    "peopleCountingDetection",
    "10089",
    "face_swapper",
    "10120",
    "SmartHumanDet",
    "10121",
    "SmartFaceDet",
    "10140",
    "PirMoveAlarm",
    "10147",
    "HumanMoveIn",
    "10148",
    "HumanLongStay",
    "10156",
    "ChildAppear",
    "12186",
    "faceCapture",
    "12187",
    "figureCapture",
    "12258",
    "CustomHumanMoveIn",
    "12259",
    "PeopleFallingDownDetection",
    "12260",
    "AllMoveOut",
    "13031",
    "DoorLock-PIRAlarm",
    "13063",
    "proneDetection",
    "13064",
    "loiter",
    "13074",
    "pirDet",
    "13078",
    "overcrowding",
    "13080",
    "FaceBlackList",
    "13210",
    "safetyHelmetDetection",
    "13218",
    "pirAlarm",
    "15004",
    "tumble_detection",
    "15007",
    "gesture_detection",
    "15011",
    "face_detect",
    "15500",
    "vas_smart_human",
    "16000",
    "StrangerAppear",
    "16400",
    "vas_face",
    "16401",
    "vas_face_dooropen",
    "50001",
    "intellectPassengerFlow",
)

_NON_MOTION_ALARM_TYPES = (
    "10016",
    "doorbell",
    "10020",
    "sceneChangeDetection",
    "10022",
    "audioException",
    "10023",
    "leftDetection",
    "10024",
    "takeDetection",
    "10155",
    "PackageAppear",
    "12002",
    "tamperDetection",
    "12086",
    "VMDRecov",
    "13202",
    "linecrossArmRecov",
    "13209",
    "fielddetectionRecov",
    "15002",
    "leave_post_detection",
    "15503",
    "Operation_prolonged_absence",
)


def _alarm(
    alarm_id: str | None,
    *,
    alarm_type: str = "motion",
    occurred_at: datetime = BASE_TIME,
    source: EventSource = EventSource.PUSH,
    image_url: str | None = "https://example.invalid/alarm.jpg",
) -> EzvizAlarmEvent:
    return EzvizAlarmEvent(
        alarm_id=alarm_id,
        device_serial=SERIAL,
        channel_no=1,
        alarm_type=alarm_type,
        occurred_at=occurred_at,
        image_url=image_url,
        source=source,
    )


def _calling(
    calling_id: str | None,
    *,
    action: CallingAction = CallingAction.RING,
    occurred_at: datetime = BASE_TIME,
    image_url: str | None = None,
) -> EzvizCallingEvent:
    return EzvizCallingEvent(
        calling_id=calling_id,
        device_serial=SERIAL,
        channel_no=1,
        action=action,
        occurred_at=occurred_at,
        image_url=image_url,
        source=EventSource.PUSH,
    )


def _reconciler(
    *,
    now=BASE_TIME,
    hold_seconds: float = 30,
    max_recent: int = 512,
    retention: timedelta = timedelta(hours=24),
) -> tuple[EventReconciler, ObservedEntityRegistry]:
    registry = ObservedEntityRegistry(HUB_ID)
    reconciler = EventReconciler(
        registry,
        now=lambda: now,
        hold_seconds=hold_seconds,
        max_recent=max_recent,
        retention=retention,
    )
    return reconciler, registry


@pytest.mark.asyncio
async def test_alarm_deduplicates_push_and_poll_and_observes_real_semantics() -> None:
    """One alarm updates state once and records only demonstrated entities."""
    reconciler, registry = _reconciler()

    assert await reconciler.async_process_alarm(_alarm("alarm-001")) is True
    assert (
        await reconciler.async_process_alarm(
            _alarm("alarm-001", source=EventSource.POLL)
        )
        is False
    )

    state = reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.motion is True
    assert state.person is False
    assert registry.keys_for(SERIAL, 1) == frozenset(
        {
            ObservedSemantic.ALARM,
            ObservedSemantic.MOTION,
            ObservedSemantic.ALARM_IMAGE,
        }
    )
    assert reconciler.recent_count == 1
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_person_alarm_observes_person_and_motion_then_resets() -> None:
    """A real person alarm justifies both downstream person and motion entities."""
    reconciler, registry = _reconciler(hold_seconds=0.01)

    assert await reconciler.async_process_alarm(
        _alarm("alarm-person", alarm_type="human_detection", image_url=None)
    )
    active = reconciler.alarm_state_for(SERIAL, 1)
    assert active is not None
    assert active.motion is True
    assert active.person is True
    assert registry.keys_for(SERIAL, 1) == frozenset(
        {
            ObservedSemantic.ALARM,
            ObservedSemantic.MOTION,
            ObservedSemantic.PERSON,
        }
    )

    await asyncio.sleep(0.03)
    reset = reconciler.alarm_state_for(SERIAL, 1)
    assert reset is not None
    assert reset.motion is False
    assert reset.person is False
    assert reconciler.active_reset_count == 0
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_numeric_and_text_motion_types_each_restart_motion() -> None:
    """Polling and Push representations independently activate one motion state."""
    reconciler, _registry = _reconciler(hold_seconds=0.01)
    try:
        assert await reconciler.async_process_alarm(
            _alarm("alarm-numeric", alarm_type="10002")
        )
        numeric_state = reconciler.alarm_state_for(SERIAL, 1)
        assert numeric_state is not None
        assert numeric_state.motion is True

        await asyncio.sleep(0.03)
        reset_state = reconciler.alarm_state_for(SERIAL, 1)
        assert reset_state is not None
        assert reset_state.motion is False

        assert await reconciler.async_process_alarm(
            _alarm(
                "alarm-text",
                alarm_type="motiondetect",
                occurred_at=BASE_TIME + timedelta(seconds=1),
            )
        )
        text_state = reconciler.alarm_state_for(SERIAL, 1)
        assert text_state is not None
        assert text_state.last_alarm_type == "motiondetect"
        assert text_state.motion is True
    finally:
        await reconciler.async_close()


@pytest.mark.asyncio
async def test_text_motion_first_remains_active_when_numeric_duplicate_arrives() -> (
    None
):
    """Transport arrival order cannot change semantics for one official alarm ID."""
    reconciler, _registry = _reconciler()
    try:
        assert await reconciler.async_process_alarm(
            _alarm("same-alarm", alarm_type="motiondetect")
        )
        assert (
            await reconciler.async_process_alarm(
                _alarm(
                    "same-alarm",
                    alarm_type="10002",
                    source=EventSource.POLL,
                )
            )
            is False
        )

        state = reconciler.alarm_state_for(SERIAL, 1)
        assert state is not None
        assert state.last_alarm_type == "motiondetect"
        assert state.motion is True
    finally:
        await reconciler.async_close()


@pytest.mark.asyncio
async def test_unknown_alarm_creates_only_generic_alarm_semantics() -> None:
    """Unknown vendor codes remain diagnostic and cannot invent detectors."""
    reconciler, registry = _reconciler()

    assert await reconciler.async_process_alarm(
        _alarm("alarm-unknown", alarm_type="doorbell_offline", image_url=None)
    )

    state = reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.motion is False
    assert state.person is False
    assert registry.keys_for(SERIAL, 1) == frozenset({ObservedSemantic.ALARM})
    await reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("alarm_type", _OFFICIAL_MOTION_ONLY_ALARM_TYPES)
async def test_official_motion_alarm_types_observe_motion_only(
    alarm_type: str,
) -> None:
    """Documented movement analytics establish motion but not person support."""
    reconciler, registry = _reconciler()
    try:
        assert await reconciler.async_process_alarm(
            _alarm("alarm-motion-type", alarm_type=alarm_type, image_url=None)
        )

        state = reconciler.alarm_state_for(SERIAL, 1)
        assert state is not None
        assert state.motion is True
        assert state.person is False
        assert registry.keys_for(SERIAL, 1) == frozenset(
            {ObservedSemantic.ALARM, ObservedSemantic.MOTION}
        )
    finally:
        await reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("alarm_type", _OFFICIAL_PERSON_ALARM_TYPES)
async def test_official_person_alarm_types_observe_person_and_motion(
    alarm_type: str,
) -> None:
    """Documented human and face analytics establish person and motion support."""
    reconciler, registry = _reconciler()
    try:
        assert await reconciler.async_process_alarm(
            _alarm("alarm-person-type", alarm_type=alarm_type, image_url=None)
        )

        state = reconciler.alarm_state_for(SERIAL, 1)
        assert state is not None
        assert state.motion is True
        assert state.person is True
        assert registry.keys_for(SERIAL, 1) == frozenset(
            {
                ObservedSemantic.ALARM,
                ObservedSemantic.MOTION,
                ObservedSemantic.PERSON,
            }
        )
    finally:
        await reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("alarm_type", _NON_MOTION_ALARM_TYPES)
async def test_non_motion_alarm_types_remain_generic(alarm_type: str) -> None:
    """Unrelated and object-state alarms do not fabricate detector semantics."""
    reconciler, registry = _reconciler()
    try:
        assert await reconciler.async_process_alarm(
            _alarm("alarm-non-motion", alarm_type=alarm_type, image_url=None)
        )

        state = reconciler.alarm_state_for(SERIAL, 1)
        assert state is not None
        assert state.motion is False
        assert state.person is False
        assert registry.keys_for(SERIAL, 1) == frozenset({ObservedSemantic.ALARM})
    finally:
        await reconciler.async_close()


@pytest.mark.asyncio
async def test_delayed_alarm_does_not_replace_newer_state() -> None:
    """A delayed poll remains deduped but cannot roll current state backward."""
    now = BASE_TIME + timedelta(minutes=10)
    reconciler, _registry = _reconciler(now=now)

    assert await reconciler.async_process_alarm(_alarm("alarm-new", occurred_at=now))
    assert (
        await reconciler.async_process_alarm(
            _alarm(
                "alarm-old",
                occurred_at=BASE_TIME,
                source=EventSource.POLL,
                image_url="https://example.invalid/old.jpg",
            )
        )
        is False
    )

    state = reconciler.alarm_state_for(SERIAL, 1)
    assert state is not None
    assert state.last_alarm_id == "alarm-new"
    assert state.last_alarm_time == now
    assert reconciler.recent_count == 2
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_ring_call_observes_doorbell_and_resets() -> None:
    """Only a real ring action creates and activates the doorbell entity."""
    reconciler, registry = _reconciler(hold_seconds=0.01)

    assert await reconciler.async_process_call(
        _calling(
            "calling-ring",
            image_url="https://example.invalid/calling.jpg",
        )
    )
    active = reconciler.calling_state_for(SERIAL, 1)
    assert active is not None
    assert active.action is CallingAction.RING
    assert active.ringing is True
    assert registry.keys_for(SERIAL, 1) == frozenset(
        {
            ObservedSemantic.CALLING,
            ObservedSemantic.DOORBELL_RING,
            ObservedSemantic.CALLING_IMAGE,
        }
    )

    await asyncio.sleep(0.03)
    reset = reconciler.calling_state_for(SERIAL, 1)
    assert reset is not None
    assert reset.ringing is False
    await reconciler.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [CallingAction.ANSWER, CallingAction.HANG_UP],
)
async def test_non_ring_call_does_not_invent_doorbell_support(
    action: CallingAction,
) -> None:
    """Answer/hang-up messages expose call status without fabricating a ring."""
    reconciler, registry = _reconciler()

    assert await reconciler.async_process_call(
        _calling("calling-status", action=action)
    )

    state = reconciler.calling_state_for(SERIAL, 1)
    assert state is not None
    assert state.action is action
    assert state.ringing is False
    assert registry.keys_for(SERIAL, 1) == frozenset({ObservedSemantic.CALLING})
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_answer_clears_an_active_ring_without_retriggering() -> None:
    """The same calling ID advances from ring to answer without dedupe loss."""
    reconciler, _registry = _reconciler(hold_seconds=60)
    await reconciler.async_process_call(_calling("calling-lifecycle"))

    assert await reconciler.async_process_call(
        _calling(
            "calling-lifecycle",
            action=CallingAction.ANSWER,
            occurred_at=BASE_TIME + timedelta(seconds=1),
        )
    )

    state = reconciler.calling_state_for(SERIAL, 1)
    assert state is not None
    assert state.action is CallingAction.ANSWER
    assert state.ringing is False
    assert reconciler.active_reset_count == 0
    await reconciler.async_close()


@pytest.mark.asyncio
async def test_recent_state_is_bounded_and_restored_without_identifiers() -> None:
    """Alarm and calling replay protection share one finite private cursor."""
    current = BASE_TIME
    reconciler, registry = _reconciler(now=current, max_recent=3)
    await reconciler.async_process_alarm(_alarm("alarm-1"))
    await reconciler.async_process_call(_calling("calling-1"))
    await reconciler.async_process_alarm(
        _alarm("alarm-2", occurred_at=BASE_TIME + timedelta(seconds=1))
    )
    await reconciler.async_process_call(
        _calling("calling-2", occurred_at=BASE_TIME + timedelta(seconds=2))
    )

    payload = reconciler.export_state()
    assert payload["version"] == 2
    assert len(payload["recent"]) == 3
    assert SERIAL not in str(payload)
    assert "https://" not in str(payload)
    await reconciler.async_close()

    restored = EventReconciler(registry, now=lambda: current, max_recent=3)
    restored.restore_state(payload)
    assert await restored.async_process_call(_calling("calling-2")) is False
    assert restored.recent_count == 3
    await restored.async_close()


@pytest.mark.asyncio
async def test_close_cancels_all_alarm_and_call_reset_tasks() -> None:
    """Config-entry unload leaves no transient event timers running."""
    reconciler, _registry = _reconciler(hold_seconds=60)
    await reconciler.async_process_alarm(_alarm("alarm-001"))
    await reconciler.async_process_call(_calling("calling-001"))

    assert reconciler.active_reset_count == 2
    await reconciler.async_close()
    assert reconciler.active_reset_count == 0
    await reconciler.async_close()
