"""Classify documented EZVIZ alarm types into Home Assistant semantics."""

from __future__ import annotations

from dataclasses import dataclass

# Official alarm type reference: https://open.ys7.com/help/981
#
# Keep this mapping conservative. An alarm establishes motion or person support
# only when the documented type describes movement, a detected subject, or a
# visual rule triggered by a subject. Other alarms remain generic events.

type AlarmTypePair = tuple[str, str]


def _documented_type_keys(pairs: tuple[AlarmTypePair, ...]) -> frozenset[str]:
    """Flatten official alarm code/name pairs into normalized lookup keys."""
    return frozenset(value.casefold() for pair in pairs for value in pair)


_MOTION_ALARM_TYPES = _documented_type_keys(
    (
        # Basic movement and visual rule triggers.
        ("10002", "motionDetect"),
        ("10013", "lineDetection"),
        ("10014", "fieldDetection"),
        ("10028", "runDetection"),
        ("10029", "enterAreaDetection"),
        ("10030", "exitAreaDetection"),
        ("10033", "intrusion"),
        ("10079", "intelligentDetection_d"),
        ("10122", "detectorMotionAlarm"),
        ("12000", "VMD"),
        ("13201", "linecrossArm"),
        ("15006", "deviceIntelligentDetection"),
        ("15010", "intelligentDetection"),
        # Vehicle and pet detections do not prove a person.
        ("10130", "car_detect"),
        ("10131", "human_car_alarm"),
        ("12185", "lpr"),
        ("15003", "pet_detection"),
        ("15501", "vas_smart_pet"),
        ("15502", "vas_smart_car"),
    )
) | frozenset(
    {
        # Stable transport aliases retained for compatibility.
        "motion",
        "motion_detection",
    }
)

_PERSON_ALARM_TYPES = _documented_type_keys(
    (
        # PIR, infrared, human, and face detections.
        ("10000", "pir"),
        ("10010", "infrared"),
        ("10015", "faceDetection"),
        ("10026", "highDensityDetection"),
        ("10027", "loiterDetection"),
        ("10035", "baby_motion"),
        ("10042", "whitelistFace"),
        ("10077", "face_stranger"),
        ("10078", "face_compare"),
        ("10088", "peopleCountingDetection"),
        ("10089", "face_swapper"),
        ("10120", "SmartHumanDet"),
        ("10121", "SmartFaceDet"),
        ("10140", "PirMoveAlarm"),
        ("10147", "HumanMoveIn"),
        ("10148", "HumanLongStay"),
        ("10156", "ChildAppear"),
        ("12186", "faceCapture"),
        ("12187", "figureCapture"),
        ("12258", "CustomHumanMoveIn"),
        ("12259", "PeopleFallingDownDetection"),
        ("12260", "AllMoveOut"),
        ("13031", "DoorLock-PIRAlarm"),
        ("13063", "proneDetection"),
        ("13064", "loiter"),
        ("13074", "pirDet"),
        ("13078", "overcrowding"),
        ("13080", "FaceBlackList"),
        ("13210", "safetyHelmetDetection"),
        ("13218", "pirAlarm"),
        ("15004", "tumble_detection"),
        ("15007", "gesture_detection"),
        ("15011", "face_detect"),
        ("15500", "vas_smart_human"),
        ("16000", "StrangerAppear"),
        ("16400", "vas_face"),
        ("16401", "vas_face_dooropen"),
        ("50001", "intellectPassengerFlow"),
    )
) | frozenset(
    {
        # Stable transport aliases retained for compatibility.
        "human",
        "human_detection",
        "person",
        "person_detection",
    }
)


@dataclass(frozen=True, slots=True)
class AlarmClassification:
    """Home Assistant semantics proven by one alarm type."""

    motion: bool
    person: bool


def classify_alarm_type(alarm_type: str) -> AlarmClassification:
    """Return conservative motion/person semantics for one vendor alarm type."""
    normalized_type = alarm_type.strip().casefold()
    is_person = normalized_type in _PERSON_ALARM_TYPES
    return AlarmClassification(
        motion=is_person or normalized_type in _MOTION_ALARM_TYPES,
        person=is_person,
    )
