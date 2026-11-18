"""Tests for X5S fixed offsets and Australian daylight-saving transitions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ezviz_iot_cn.x5s_time import (
    RecorderTime,
    X5SClient,
    melbourne_option,
)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2027-04-03T15:59:59+00:00", "utc_plus_11"),
        ("2027-04-03T16:00:00+00:00", "utc_plus_10"),
        ("2027-10-02T15:59:59+00:00", "utc_plus_10"),
        ("2027-10-02T16:00:00+00:00", "utc_plus_11"),
    ],
)
def test_melbourne_transition(instant: str, expected: str) -> None:
    """Use the new offset exactly at each local transition."""
    assert melbourne_option(datetime.fromisoformat(instant)) == expected


def _clock(hours: int, zone: str = "EST-10") -> RecorderTime:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    return RecorderTime(
        zone, "NTP", False, now, now.replace(tzinfo=None) + timedelta(hours=hours)
    )


async def test_matching_offset_needs_no_write() -> None:
    """Avoid sending a command that could temporarily shift the recorder clock."""
    client = Mock(spec=X5SClient)
    current = _clock(10)

    assert await X5SClient.set_offset(client, "utc_plus_10", current) is current
    client._request.assert_not_called()
    client.read_time.assert_not_called()


async def test_read_back_offset_even_after_fault() -> None:
    """Read back even if firmware reports a fault after applying the setting."""
    client = Mock(spec=X5SClient)
    client._request = AsyncMock(side_effect=HomeAssistantError("SOAP fault"))
    client.read_time = AsyncMock(return_value=_clock(11, "SBT-11"))

    result = await X5SClient.set_offset(client, "utc_plus_11", _clock(10))

    assert result.fixed_option == "utc_plus_11"
    client._request.assert_awaited_once_with(
        "SetSystemDateAndTime", authenticate=True, zone="SBT-11"
    )
