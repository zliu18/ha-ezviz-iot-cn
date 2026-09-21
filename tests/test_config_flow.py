"""Tests for EZVIZ IoT CN UI configuration flows."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import config_validation as cv
from probatio.codecs.fields import to_field_list
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_iot_cn.api.errors import (
    EzvizAuthError,
    EzvizConnectionError,
    EzvizRateLimitError,
)
from custom_components.ezviz_iot_cn.api.models import (
    AlarmSoundMode,
    EzvizCapabilities,
    EzvizChannel,
    EzvizDevice,
    hub_uid,
)
from custom_components.ezviz_iot_cn.const import (
    CONF_ALARM_INTERVAL,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_CAPTURE_INTERVAL,
    CONF_DEVICE_FILTER_MODE,
    CONF_ENABLE_ALARM_IMAGES,
    CONF_ENABLE_ALARM_POLLING,
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_ENABLE_SCHEDULED_CAPTURES,
    CONF_INCLUDE_SHARED_DEVICES,
    CONF_METADATA_INTERVAL,
    CONF_MOTION_HOLD_SECONDS,
    CONF_SELECTED_DEVICES,
    CONF_STATE_INTERVAL,
    CONF_WEBHOOK_ID,
    DEFAULT_ALARM_INTERVAL,
    DEFAULT_CAPTURE_INTERVAL,
    DEFAULT_METADATA_INTERVAL,
    DEFAULT_MOTION_HOLD_SECONDS,
    DEFAULT_STATE_INTERVAL,
    DOMAIN,
    FILTER_MODE_EXCLUDE,
    FILTER_MODE_INCLUDE,
    OPEN_PLATFORM_API_DOCS_URL,
    OPEN_PLATFORM_MESSAGE_PUSH_CONSOLE_URL,
    OPEN_PLATFORM_MESSAGE_PUSH_DOCS_URL,
)
from custom_components.ezviz_iot_cn.coordinator import MetadataSnapshot

APP_KEY = "synthetic-app-key"
RAW_APP_KEY = f" {APP_KEY} "
APP_SECRET = "synthetic-app-secret"
SERIAL_1 = "SYNTHETIC-SERIAL-001"
SERIAL_2 = "SYNTHETIC-SERIAL-002"
HUB_NAME = "hub_name"


def _device(serial: str) -> EzvizDevice:
    return EzvizDevice(
        serial=serial,
        name=f"Camera {serial[-1]}",
        model="CS-SYNTHETIC",
        online=True,
        defence=1,
        alarm_sound_mode=AlarmSoundMode.SILENT,
        firmware_version="V1.0.0",
    )


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )


@pytest.mark.asyncio
async def test_user_flow_creates_hub_with_stable_app_key_identity(
    hass,
    enable_custom_integrations,
) -> None:
    """Credentials are validated and only AppKey participates in identity."""
    session = object()
    with (
        patch(
            "custom_components.ezviz_iot_cn.config_flow.async_get_clientsession",
            return_value=session,
        ),
        patch(
            "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
        ) as client_class,
    ):
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await _start_user_flow(hass)
        assert initial["last_step"] is False
        assert initial["description_placeholders"] == {
            "api_docs_url": OPEN_PLATFORM_API_DOCS_URL,
        }
        generated_hub_name = next(
            marker.default()
            for marker in initial["data_schema"].schema
            if marker.schema == HUB_NAME
        )
        devices_step = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {
                CONF_APP_KEY: APP_KEY,
                CONF_APP_SECRET: APP_SECRET,
                CONF_INCLUDE_SHARED_DEVICES: True,
                HUB_NAME: "Front Door Hub",
            },
        )
        result = await hass.config_entries.flow.async_configure(
            devices_step["flow_id"],
            {
                CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
                CONF_SELECTED_DEVICES: [],
            },
        )

    assert devices_step["type"] is data_entry_flow.FlowResultType.FORM
    assert devices_step["step_id"] == "devices"
    assert devices_step["last_step"] is True
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert re.fullmatch(r"EZVIZ IoT CN Hub [A-Z0-9]{2}", generated_hub_name)
    assert result["title"] == "Front Door Hub"
    assert result["data"] == {
        CONF_APP_KEY: APP_KEY,
        CONF_APP_SECRET: APP_SECRET,
    }
    assert result["options"] == {
        CONF_INCLUDE_SHARED_DEVICES: True,
        CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
        CONF_SELECTED_DEVICES: [],
    }
    assert result["result"].unique_id == hub_uid(APP_KEY)
    client_class.assert_called_once_with(
        session,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
    )
    client_class.return_value.async_validate_credentials.assert_awaited_once_with(
        include_shared_devices=True
    )


@pytest.mark.asyncio
async def test_user_flow_opens_with_existing_pre_release_hub(
    hass,
    enable_custom_integrations,
) -> None:
    """An existing pre-release entry must not prevent opening a new flow."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        title="EZVIZ IoT CN",
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        options={},
        unique_id=hub_uid(APP_KEY),
    )
    existing.add_to_hass(hass)

    result = await _start_user_flow(hass)

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    generated_hub_name = next(
        marker.default()
        for marker in result["data_schema"].schema
        if marker.schema == HUB_NAME
    )
    assert re.fullmatch(r"EZVIZ IoT CN Hub [A-Z0-9]{2}", generated_hub_name)
    fields = to_field_list(
        result["data_schema"],
        custom_serializer=cv.custom_serializer,
    )
    assert any(field["name"] == HUB_NAME for field in fields)


@pytest.mark.asyncio
async def test_user_flow_rejects_invalid_hub_name_on_the_form(
    hass,
    enable_custom_integrations,
) -> None:
    """Invalid Hub names remain field errors instead of breaking the flow."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        initial = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {
                CONF_APP_KEY: APP_KEY,
                CONF_APP_SECRET: APP_SECRET,
                CONF_INCLUDE_SHARED_DEVICES: True,
                HUB_NAME: "   ",
            },
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {HUB_NAME: "invalid_hub_name"}
    client_class.assert_not_called()


@pytest.mark.asyncio
async def test_options_allow_hub_name_to_be_changed(
    hass,
    enable_custom_integrations,
) -> None:
    """Hub options expose and persist the user-facing config-entry title."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EZVIZ IoT CN Hub A7",
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.options.async_init(entry.entry_id)
        fields = to_field_list(
            initial["data_schema"],
            custom_serializer=cv.custom_serializer,
        )
        defaults = {
            marker.schema: marker.default()
            for marker in initial["data_schema"].schema
            if callable(marker.default)
        }
        updated = dict(defaults)
        updated[HUB_NAME] = "Courtyard Cameras"
        result = await hass.config_entries.options.async_configure(
            initial["flow_id"],
            updated,
        )

    assert any(field["name"] == HUB_NAME for field in fields)
    assert defaults[HUB_NAME] == "EZVIZ IoT CN Hub A7"
    assert HUB_NAME not in result["data"]
    assert entry.title == "Courtyard Cameras"


@pytest.mark.asyncio
async def test_options_reject_invalid_hub_name_without_updating_title(
    hass,
    enable_custom_integrations,
) -> None:
    """Options report an invalid Hub name without mutating the entry title."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EZVIZ IoT CN Hub A7",
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {
            marker.schema: marker.default()
            for marker in initial["data_schema"].schema
            if callable(marker.default)
        }
        invalid = dict(defaults)
        invalid[HUB_NAME] = "   "
        result = await hass.config_entries.options.async_configure(
            initial["flow_id"],
            invalid,
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {HUB_NAME: "invalid_hub_name"}
    assert entry.title == "EZVIZ IoT CN Hub A7"


@pytest.mark.asyncio
async def test_user_flow_defaults_to_shared_devices_before_preloading_selection(
    hass,
    enable_custom_integrations,
) -> None:
    """The first-screen default must control discovery before device selection."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await _start_user_flow(hass)
        defaults = {
            marker.schema: marker.default()
            for marker in initial["data_schema"].schema
            if callable(marker.default)
        }
        devices_step = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {
                CONF_APP_KEY: APP_KEY,
                CONF_APP_SECRET: APP_SECRET,
                CONF_INCLUDE_SHARED_DEVICES: defaults[CONF_INCLUDE_SHARED_DEVICES],
            },
        )

    assert defaults[CONF_INCLUDE_SHARED_DEVICES] is True
    assert devices_step["step_id"] == "devices"
    device_selector = next(
        selector
        for marker, selector in devices_step["data_schema"].schema.items()
        if marker.schema == CONF_SELECTED_DEVICES
    )
    assert device_selector.config["options"] == [
        {"value": SERIAL_1, "label": "Camera 1 (CS-SYNTHETIC)"}
    ]
    client_class.return_value.async_validate_credentials.assert_awaited_once_with(
        include_shared_devices=True
    )


@pytest.mark.asyncio
async def test_user_flow_can_exclude_shared_devices_before_preloading_selection(
    hass,
    enable_custom_integrations,
) -> None:
    """Disabling shared discovery is applied before the device-selection step."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await _start_user_flow(hass)
        devices_step = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {
                CONF_APP_KEY: APP_KEY,
                CONF_APP_SECRET: APP_SECRET,
                CONF_INCLUDE_SHARED_DEVICES: False,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            devices_step["flow_id"],
            {
                CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
                CONF_SELECTED_DEVICES: [],
            },
        )

    client_class.return_value.async_validate_credentials.assert_awaited_once_with(
        include_shared_devices=False
    )
    assert result["options"][CONF_INCLUDE_SHARED_DEVICES] is False


@pytest.mark.asyncio
async def test_user_flow_preserves_app_key_credential_bytes(
    hass,
    enable_custom_integrations,
) -> None:
    """Identity normalization must not rewrite the API credential."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await _start_user_flow(hass)
        devices_step = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: RAW_APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )
        result = await hass.config_entries.flow.async_configure(
            devices_step["flow_id"],
            {
                CONF_DEVICE_FILTER_MODE: FILTER_MODE_INCLUDE,
                CONF_SELECTED_DEVICES: [SERIAL_1],
            },
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APP_KEY] == RAW_APP_KEY
    assert result["result"].unique_id == hub_uid(APP_KEY)
    assert client_class.call_args.kwargs["app_key"] == RAW_APP_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EzvizAuthError("rejected"), "invalid_auth"),
        (EzvizConnectionError("offline"), "cannot_connect"),
        (EzvizRateLimitError("limited"), "rate_limited"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_flow_maps_errors_without_storing_credentials(
    hass,
    enable_custom_integrations,
    error: Exception,
    expected: str,
) -> None:
    """All failures remain on the form with translated secret-free errors."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            side_effect=error
        )
        initial = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert result["last_step"] is False
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_user_flow_can_retry_with_corrected_credentials(
    hass,
    enable_custom_integrations,
) -> None:
    """A failed credential attempt does not trap the flow on its first AppKey."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            side_effect=(EzvizAuthError("rejected"), (_device(SERIAL_2),))
        )
        initial = await _start_user_flow(hass)
        failed = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: "mistyped-app-key", CONF_APP_SECRET: "bad-secret"},
        )
        devices_step = await hass.config_entries.flow.async_configure(
            failed["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )

    assert failed["errors"] == {"base": "invalid_auth"}
    assert failed["last_step"] is False
    assert devices_step["step_id"] == "devices"
    assert devices_step["last_step"] is True


@pytest.mark.asyncio
async def test_user_flow_allows_empty_account_for_future_devices(
    hass,
    enable_custom_integrations,
) -> None:
    """Exclude-none setup remains valid when the application has no devices yet."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=()
        )
        initial = await _start_user_flow(hass)
        devices_step = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )
        result = await hass.config_entries.flow.async_configure(
            devices_step["flow_id"],
            {
                CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
                CONF_SELECTED_DEVICES: [],
            },
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["options"] == {
        CONF_INCLUDE_SHARED_DEVICES: True,
        CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
        CONF_SELECTED_DEVICES: [],
    }


@pytest.mark.asyncio
async def test_in_progress_device_selection_blocks_duplicate_app_key(
    hass,
    enable_custom_integrations,
) -> None:
    """A second flow cannot race a validated Hub waiting on device selection."""
    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        first = await _start_user_flow(hass)
        first_devices = await hass.config_entries.flow.async_configure(
            first["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )
        second = await _start_user_flow(hass)
        duplicate = await hass.config_entries.flow.async_configure(
            second["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: "different-secret"},
        )

    assert first_devices["step_id"] == "devices"
    assert duplicate["type"] is data_entry_flow.FlowResultType.ABORT
    assert duplicate["reason"] == "already_in_progress"
    assert client_class.return_value.async_validate_credentials.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_app_key_aborts_before_network_access(
    hass,
    enable_custom_integrations,
) -> None:
    """A second secret cannot create a competing hub for the same AppKey."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: "old-secret"},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        initial = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: "different-secret"},
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    client_class.assert_not_called()


@pytest.mark.asyncio
async def test_reauth_updates_secret_without_changing_identity(
    hass,
    enable_custom_integrations,
) -> None:
    """Credential rotation keeps the existing hub and entity identity tree."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APP_KEY: APP_KEY,
            CONF_APP_SECRET: "old-secret",
            CONF_WEBHOOK_ID: "ezviz_iot_cn_stable-webhook",
        },
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: "new-secret"},
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == hub_uid(APP_KEY)
    assert entry.data[CONF_APP_SECRET] == "new-secret"
    assert entry.data[CONF_WEBHOOK_ID] == "ezviz_iot_cn_stable-webhook"
    reload_entry.assert_not_called()


@pytest.mark.asyncio
async def test_reauth_preserves_app_key_credential_bytes(
    hass,
    enable_custom_integrations,
) -> None:
    """Reauthentication normalizes identity without rewriting AppKey."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: "old-secret"},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: RAW_APP_KEY, CONF_APP_SECRET: "new-secret"},
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == hub_uid(APP_KEY)
    assert entry.data[CONF_APP_KEY] == RAW_APP_KEY
    assert client_class.call_args.kwargs["app_key"] == RAW_APP_KEY


@pytest.mark.asyncio
async def test_reconfigure_rejects_a_different_app_key(
    hass,
    enable_custom_integrations,
) -> None:
    """Reconfiguration cannot silently replace one logical hub with another."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    initial = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(
        initial["flow_id"],
        {CONF_APP_KEY: "different-app-key", CONF_APP_SECRET: APP_SECRET},
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


@pytest.mark.asyncio
async def test_reconfigure_preserves_app_key_credential_bytes(
    hass,
    enable_custom_integrations,
) -> None:
    """Reconfigure keeps raw credentials for the same normalized hub."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APP_KEY: APP_KEY,
            CONF_APP_SECRET: APP_SECRET,
            CONF_WEBHOOK_ID: "ezviz_iot_cn_stable-webhook",
        },
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            {CONF_APP_KEY: RAW_APP_KEY, CONF_APP_SECRET: APP_SECRET},
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == hub_uid(APP_KEY)
    assert entry.data[CONF_APP_KEY] == RAW_APP_KEY
    assert entry.data[CONF_WEBHOOK_ID] == "ezviz_iot_cn_stable-webhook"
    assert client_class.call_args.kwargs["app_key"] == RAW_APP_KEY


@pytest.mark.asyncio
async def test_options_store_complete_bounded_behavior_without_credentials(
    hass,
    enable_custom_integrations,
) -> None:
    """Options discover devices and store only validated behavior choices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)
    choices = {
        CONF_INCLUDE_SHARED_DEVICES: True,
        CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
        CONF_ENABLE_ALARM_POLLING: True,
        CONF_ALARM_INTERVAL: 30,
        CONF_STATE_INTERVAL: 120,
        CONF_METADATA_INTERVAL: 3600,
        CONF_ENABLE_SCHEDULED_CAPTURES: True,
        CONF_CAPTURE_INTERVAL: 900,
        CONF_ENABLE_ALARM_IMAGES: False,
        CONF_MOTION_HOLD_SECONDS: 45,
        CONF_ENABLE_NATIVE_WEBHOOK: False,
        CONF_SELECTED_DEVICES: [SERIAL_2],
    }

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1), _device(SERIAL_2))
        )
        initial = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            initial["flow_id"],
            choices,
        )

    assert initial["type"] is data_entry_flow.FlowResultType.FORM
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == choices
    assert CONF_APP_KEY not in result["data"]
    assert CONF_APP_SECRET not in result["data"]


@pytest.mark.asyncio
async def test_options_show_current_private_webhook_url(
    hass,
    enable_custom_integrations,
) -> None:
    """The authenticated options UI exposes the bearer-secret URL for setup."""
    webhook_id = "ezviz_iot_cn_synthetic-private-id"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_APP_KEY: APP_KEY,
            CONF_APP_SECRET: APP_SECRET,
            CONF_WEBHOOK_ID: webhook_id,
        },
        options={CONF_ENABLE_NATIVE_WEBHOOK: True},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
        ) as client_class,
        patch(
            "custom_components.ezviz_iot_cn.config_flow.ha_webhook.async_generate_url",
            return_value=f"https://ha.example.test/api/webhook/{webhook_id}",
        ) as generate_url,
    ):
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["description_placeholders"] == {
        "api_docs_url": OPEN_PLATFORM_API_DOCS_URL,
        "message_push_docs_url": OPEN_PLATFORM_MESSAGE_PUSH_DOCS_URL,
        "message_push_console_url": OPEN_PLATFORM_MESSAGE_PUSH_CONSOLE_URL,
        "webhook_url": f"https://ha.example.test/api/webhook/{webhook_id}",
        "estimated_daily_api_calls": "11536",
        "estimated_daily_image_downloads": "0",
        "included_device_count": "1",
        "capture_channel_count": "0",
    }
    generate_url.assert_called_once_with(hass, webhook_id)


@pytest.mark.asyncio
async def test_options_show_placeholder_before_webhook_is_generated(
    hass,
    enable_custom_integrations,
) -> None:
    """The first enablement explains that a URL appears after reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"] == {
        "api_docs_url": OPEN_PLATFORM_API_DOCS_URL,
        "message_push_docs_url": OPEN_PLATFORM_MESSAGE_PUSH_DOCS_URL,
        "message_push_console_url": OPEN_PLATFORM_MESSAGE_PUSH_CONSOLE_URL,
        "webhook_url": "-",
        "estimated_daily_api_calls": "11536",
        "estimated_daily_image_downloads": "0",
        "included_device_count": "1",
        "capture_channel_count": "0",
    }


@pytest.mark.asyncio
async def test_options_estimate_uses_only_verified_capture_channels(
    hass,
    enable_custom_integrations,
) -> None:
    """The UI estimate uses runtime capabilities and never guesses support."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        options={
            CONF_DEVICE_FILTER_MODE: FILTER_MODE_INCLUDE,
            CONF_SELECTED_DEVICES: [SERIAL_1],
            CONF_ENABLE_ALARM_POLLING: False,
            CONF_STATE_INTERVAL: 120,
            CONF_METADATA_INTERVAL: 43200,
            CONF_ENABLE_SCHEDULED_CAPTURES: True,
            CONF_CAPTURE_INTERVAL: 600,
        },
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        metadata_coordinator=SimpleNamespace(
            data=MetadataSnapshot(
                devices=(_device(SERIAL_1), _device(SERIAL_2)),
                channels=(
                    EzvizChannel(SERIAL_1, 1, "Channel 1", True, True),
                    EzvizChannel(SERIAL_1, 2, "Channel 2", True, True),
                    EzvizChannel(SERIAL_2, 1, "Channel 1", True, True),
                ),
                capabilities=(
                    EzvizCapabilities(SERIAL_1, True, True, False, False, False),
                    EzvizCapabilities(SERIAL_2, False, True, False, False, False),
                ),
            )
        )
    )

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1), _device(SERIAL_2))
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"] == {
        "api_docs_url": OPEN_PLATFORM_API_DOCS_URL,
        "message_push_docs_url": OPEN_PLATFORM_MESSAGE_PUSH_DOCS_URL,
        "message_push_console_url": OPEN_PLATFORM_MESSAGE_PUSH_CONSOLE_URL,
        "webhook_url": "-",
        "estimated_daily_api_calls": "1736",
        "estimated_daily_image_downloads": "288",
        "included_device_count": "1",
        "capture_channel_count": "2",
    }


@pytest.mark.asyncio
async def test_options_defaults_and_interval_bounds(
    hass,
    enable_custom_integrations,
) -> None:
    """Defaults are explicit and unsafe polling intervals are rejected."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1),)
        )
        initial = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {
            marker.schema: marker.default()
            for marker in initial["data_schema"].schema
            if callable(marker.default)
        }
        invalid = dict(defaults)
        invalid[CONF_ALARM_INTERVAL] = 1
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                initial["flow_id"],
                invalid,
            )

    assert defaults[CONF_ALARM_INTERVAL] == DEFAULT_ALARM_INTERVAL
    assert defaults[CONF_STATE_INTERVAL] == DEFAULT_STATE_INTERVAL
    assert defaults[CONF_METADATA_INTERVAL] == DEFAULT_METADATA_INTERVAL
    assert defaults[CONF_CAPTURE_INTERVAL] == DEFAULT_CAPTURE_INTERVAL
    assert defaults[CONF_MOTION_HOLD_SECONDS] == DEFAULT_MOTION_HOLD_SECONDS
    assert defaults[CONF_INCLUDE_SHARED_DEVICES] is True
    assert defaults[CONF_DEVICE_FILTER_MODE] == FILTER_MODE_EXCLUDE
    assert defaults[CONF_SELECTED_DEVICES] == []


@pytest.mark.asyncio
async def test_options_estimate_uses_effective_excluded_devices(
    hass,
    enable_custom_integrations,
) -> None:
    """Quota guidance counts included devices rather than raw selections."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_KEY: APP_KEY, CONF_APP_SECRET: APP_SECRET},
        options={
            CONF_DEVICE_FILTER_MODE: FILTER_MODE_EXCLUDE,
            CONF_SELECTED_DEVICES: [SERIAL_1],
            CONF_ENABLE_ALARM_POLLING: False,
            CONF_STATE_INTERVAL: 120,
            CONF_METADATA_INTERVAL: 43200,
        },
        unique_id=hub_uid(APP_KEY),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ezviz_iot_cn.config_flow.EzvizApiClient"
    ) as client_class:
        client_class.return_value.async_validate_credentials = AsyncMock(
            return_value=(_device(SERIAL_1), _device(SERIAL_2))
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"]["included_device_count"] == "1"
