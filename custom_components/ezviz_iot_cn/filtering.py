"""Shared device-filter semantics for one EZVIZ hub."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    CONF_DEVICE_FILTER_MODE,
    CONF_INCLUDE_SHARED_DEVICES,
    CONF_SELECTED_DEVICES,
    DEFAULT_DEVICE_FILTER_MODE,
    FILTER_MODE_EXCLUDE,
    FILTER_MODE_INCLUDE,
)


def include_shared_devices(options: Mapping[str, Any]) -> bool:
    """Return whether shared/managed devices participate in this hub."""
    value = options.get(CONF_INCLUDE_SHARED_DEVICES, True)
    return value if isinstance(value, bool) else True


def device_filter_mode(options: Mapping[str, Any]) -> str:
    """Return the explicit mode or preserve the legacy include-list behavior."""
    mode = options.get(CONF_DEVICE_FILTER_MODE)
    if mode in {FILTER_MODE_INCLUDE, FILTER_MODE_EXCLUDE}:
        return str(mode)
    if CONF_DEVICE_FILTER_MODE not in options and CONF_SELECTED_DEVICES in options:
        return FILTER_MODE_INCLUDE
    return DEFAULT_DEVICE_FILTER_MODE


def configured_device_serials(options: Mapping[str, Any]) -> frozenset[str]:
    """Return a bounded, normalized set of configured serial values."""
    selected = options.get(CONF_SELECTED_DEVICES, ())
    if isinstance(selected, str) or not isinstance(selected, Iterable):
        return frozenset()
    return frozenset(str(serial) for serial in selected)


def device_is_included(options: Mapping[str, Any], serial: str) -> bool:
    """Return whether one serial is included by the hub filter."""
    selected = configured_device_serials(options)
    if device_filter_mode(options) == FILTER_MODE_INCLUDE:
        return serial in selected
    return serial not in selected


def effective_device_serials(
    options: Mapping[str, Any],
    available_serials: Iterable[str],
) -> set[str]:
    """Return available serials included by the configured mode."""
    available = {str(serial) for serial in available_serials}
    selected = configured_device_serials(options)
    if device_filter_mode(options) == FILTER_MODE_INCLUDE:
        return available & selected
    return available - selected
