"""Tests for shared device-filter semantics."""

from custom_components.ezviz_iot_cn.const import (
    CONF_DEVICE_FILTER_MODE,
    CONF_SELECTED_DEVICES,
    FILTER_MODE_EXCLUDE,
    FILTER_MODE_INCLUDE,
)
from custom_components.ezviz_iot_cn.filtering import (
    device_is_included,
    effective_device_serials,
)

SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"


def test_unconfigured_filter_includes_current_and_future_devices() -> None:
    """An empty new configuration behaves as exclude-none, not include-none."""
    options: dict[str, object] = {}

    assert effective_device_serials(options, {SERIAL_1, SERIAL_2}) == {
        SERIAL_1,
        SERIAL_2,
    }
    assert device_is_included(options, "SYNTHETIC-FUTURE-SERIAL") is True


def test_legacy_selected_devices_keep_include_semantics() -> None:
    """Existing entries without a mode retain their previous allowlist behavior."""
    options = {CONF_SELECTED_DEVICES: [SERIAL_1]}

    assert effective_device_serials(options, {SERIAL_1, SERIAL_2}) == {SERIAL_1}
    assert device_is_included(options, SERIAL_1) is True
    assert device_is_included(options, SERIAL_2) is False


def test_explicit_include_and_exclude_modes_use_the_same_selected_list() -> None:
    """The mode alone decides whether selected serials are kept or omitted."""
    include = {
        CONF_DEVICE_FILTER_MODE: FILTER_MODE_INCLUDE,
        CONF_SELECTED_DEVICES: [SERIAL_1],
    }
    exclude = {
        CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
        CONF_SELECTED_DEVICES: [SERIAL_1],
    }

    assert effective_device_serials(include, {SERIAL_1, SERIAL_2}) == {SERIAL_1}
    assert effective_device_serials(exclude, {SERIAL_1, SERIAL_2}) == {SERIAL_2}
    assert device_is_included(include, SERIAL_2) is False
    assert device_is_included(exclude, SERIAL_2) is True
