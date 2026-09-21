"""UI configuration for EZVIZ IoT CN."""

from __future__ import annotations

import random
import string
from collections.abc import Iterable, Mapping
from typing import Any, override

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import webhook as ha_webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api.client import EzvizApiClient
from .api.errors import EzvizAuthError, EzvizConnectionError, EzvizRateLimitError
from .api.models import EzvizDevice, hub_uid
from .const import (
    CONF_ALARM_INTERVAL,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_CAPTURE_INTERVAL,
    CONF_DEVICE_FILTER_MODE,
    CONF_ENABLE_ALARM_IMAGES,
    CONF_ENABLE_ALARM_POLLING,
    CONF_ENABLE_NATIVE_WEBHOOK,
    CONF_ENABLE_SCHEDULED_CAPTURES,
    CONF_HUB_NAME,
    CONF_INCLUDE_SHARED_DEVICES,
    CONF_METADATA_INTERVAL,
    CONF_MOTION_HOLD_SECONDS,
    CONF_SELECTED_DEVICES,
    CONF_STATE_INTERVAL,
    CONF_WEBHOOK_ID,
    DEFAULT_ALARM_INTERVAL,
    DEFAULT_CAPTURE_INTERVAL,
    DEFAULT_DEVICE_FILTER_MODE,
    DEFAULT_METADATA_INTERVAL,
    DEFAULT_MOTION_HOLD_SECONDS,
    DEFAULT_STATE_INTERVAL,
    DOMAIN,
    FILTER_MODE_EXCLUDE,
    FILTER_MODE_INCLUDE,
    MAX_ALARM_INTERVAL,
    MAX_CAPTURE_INTERVAL,
    MAX_METADATA_INTERVAL,
    MAX_MOTION_HOLD_SECONDS,
    MAX_STATE_INTERVAL,
    MIN_ALARM_INTERVAL,
    MIN_CAPTURE_INTERVAL,
    MIN_METADATA_INTERVAL,
    MIN_MOTION_HOLD_SECONDS,
    MIN_STATE_INTERVAL,
)
from .coordinator import MetadataSnapshot
from .filtering import (
    configured_device_serials,
    device_filter_mode,
    effective_device_serials,
    include_shared_devices,
)
from .quota import estimate_daily_usage

_TITLE = "EZVIZ IoT CN"
_HUB_NAME_PREFIX = f"{_TITLE} Hub"
_HUB_NAME_SUFFIX_LENGTH = 2
_MAX_HUB_NAME_LENGTH = 64


def _generate_hub_name(existing_names: Iterable[str]) -> str:
    """Return a short HomeKit-style Hub name not used by another entry."""
    unavailable = set(existing_names)
    alphabet = string.ascii_uppercase + string.digits
    while True:
        suffix = "".join(random.choices(alphabet, k=_HUB_NAME_SUFFIX_LENGTH))
        candidate = f"{_HUB_NAME_PREFIX} {suffix}"
        if candidate not in unavailable:
            return candidate


def _normalize_hub_name(value: Any) -> str:
    """Return a bounded non-empty Hub display name."""
    name = str(value).strip()
    if not name:
        raise ValueError("Hub name cannot be empty")
    if len(name) > _MAX_HUB_NAME_LENGTH:
        raise ValueError(f"Hub name cannot exceed {_MAX_HUB_NAME_LENGTH} characters")
    return name


def _credentials_schema(
    suggested: Mapping[str, Any] | None = None,
    *,
    default_hub_name: str | None = None,
    show_shared_device_option: bool = False,
) -> vol.Schema:
    app_key_marker: vol.Marker
    if suggested and suggested.get(CONF_APP_KEY):
        app_key_marker = vol.Required(
            CONF_APP_KEY,
            default=str(suggested[CONF_APP_KEY]),
        )
    else:
        app_key_marker = vol.Required(CONF_APP_KEY)
    schema: dict[vol.Marker, object] = {}
    if default_hub_name is not None:
        schema[
            vol.Optional(
                CONF_HUB_NAME,
                default=str(
                    suggested.get(CONF_HUB_NAME, default_hub_name)
                    if suggested
                    else default_hub_name
                ),
            )
        ] = TextSelector(TextSelectorConfig(autocomplete="off"))
    schema.update(
        {
            app_key_marker: TextSelector(TextSelectorConfig(autocomplete="off")),
            vol.Required(CONF_APP_SECRET): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="off",
                )
            ),
        }
    )
    if show_shared_device_option:
        schema[
            vol.Optional(
                CONF_INCLUDE_SHARED_DEVICES,
                default=bool(
                    True
                    if suggested is None
                    else suggested.get(CONF_INCLUDE_SHARED_DEVICES, True)
                ),
            )
        ] = BooleanSelector()
    return vol.Schema(schema)


async def _async_validate_credentials(
    flow: config_entries.ConfigFlow | config_entries.OptionsFlow,
    app_key: str,
    app_secret: str,
    *,
    include_shared_devices: bool,
) -> tuple[EzvizDevice, ...]:
    client = EzvizApiClient(
        async_get_clientsession(flow.hass),
        app_key=app_key,
        app_secret=app_secret,
    )
    return await client.async_validate_credentials(
        include_shared_devices=include_shared_devices
    )


def _error_key(error: Exception) -> str:
    if isinstance(error, EzvizAuthError):
        return "invalid_auth"
    if isinstance(error, EzvizRateLimitError):
        return "rate_limited"
    if isinstance(error, EzvizConnectionError):
        return "cannot_connect"
    return "unknown"


class EzvizIotCnConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one EZVIZ China Open Platform application hub."""

    VERSION = 1
    _pending_app_key: str | None = None
    _pending_app_secret: str | None = None
    _pending_devices: tuple[EzvizDevice, ...] = ()
    _pending_hub_name: str | None = None
    _pending_include_shared_devices = True
    _suggested_hub_name: str | None = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        _config_entry: ConfigEntry,
    ) -> EzvizIotCnOptionsFlow:
        """Return the integration options flow."""
        return EzvizIotCnOptionsFlow()

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure a new AppKey/AppSecret hub."""
        errors: dict[str, str] = {}
        if self._suggested_hub_name is None:
            self._suggested_hub_name = _generate_hub_name(
                entry.title for entry in self._async_current_entries()
            )
        if user_input is not None:
            try:
                hub_name = _normalize_hub_name(user_input[CONF_HUB_NAME])
            except ValueError:
                errors[CONF_HUB_NAME] = "invalid_hub_name"
            else:
                app_key = str(user_input[CONF_APP_KEY])
                app_secret = str(user_input[CONF_APP_SECRET])
                include_shared = bool(user_input.get(CONF_INCLUDE_SHARED_DEVICES, True))
                await self.async_set_unique_id(hub_uid(app_key), raise_on_progress=True)
                self._abort_if_unique_id_configured()
                try:
                    devices = await _async_validate_credentials(
                        self,
                        app_key,
                        app_secret,
                        include_shared_devices=include_shared,
                    )
                except Exception as err:  # API errors are mapped without payloads.
                    errors["base"] = _error_key(err)
                else:
                    self._pending_app_key = app_key
                    self._pending_app_secret = app_secret
                    self._pending_devices = devices
                    self._pending_hub_name = hub_name
                    self._pending_include_shared_devices = include_shared
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(
                user_input,
                default_hub_name=self._suggested_hub_name,
                show_shared_device_option=True,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_devices(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose which discovered devices belong to this hub entry."""
        if (
            self._pending_app_key is None
            or self._pending_app_secret is None
            or self._pending_hub_name is None
        ):
            return self.async_abort(reason="missing_credentials")
        if user_input is not None:
            return self.async_create_entry(
                title=self._pending_hub_name,
                data={
                    CONF_APP_KEY: self._pending_app_key,
                    CONF_APP_SECRET: self._pending_app_secret,
                },
                options={
                    CONF_INCLUDE_SHARED_DEVICES: self._pending_include_shared_devices,
                    CONF_DEVICE_FILTER_MODE: str(user_input[CONF_DEVICE_FILTER_MODE]),
                    CONF_SELECTED_DEVICES: list(
                        user_input.get(CONF_SELECTED_DEVICES, [])
                    ),
                },
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=_device_filter_schema(
                self._pending_devices,
                mode=DEFAULT_DEVICE_FILTER_MODE,
                selected=(),
            ),
            last_step=True,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start credential reauthentication."""
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate replacement credentials without changing hub identity."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            app_key = str(user_input[CONF_APP_KEY])
            app_secret = str(user_input[CONF_APP_SECRET])
            await self.async_set_unique_id(hub_uid(app_key), raise_on_progress=False)
            self._abort_if_unique_id_mismatch()
            try:
                await _async_validate_credentials(
                    self,
                    app_key,
                    app_secret,
                    include_shared_devices=include_shared_devices(entry.options),
                )
            except Exception as err:
                errors["base"] = _error_key(err)
            else:
                return self.async_update_and_abort(
                    entry,
                    data_updates={
                        CONF_APP_KEY: app_key,
                        CONF_APP_SECRET: app_secret,
                    },
                )

        suggested = user_input or {
            CONF_APP_KEY: entry.data[CONF_APP_KEY],
            CONF_APP_SECRET: "",
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(suggested),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and replace credentials for the same logical hub."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            app_key = str(user_input[CONF_APP_KEY])
            app_secret = str(user_input[CONF_APP_SECRET])
            await self.async_set_unique_id(hub_uid(app_key), raise_on_progress=False)
            self._abort_if_unique_id_mismatch()
            try:
                await _async_validate_credentials(
                    self,
                    app_key,
                    app_secret,
                    include_shared_devices=include_shared_devices(entry.options),
                )
            except Exception as err:
                errors["base"] = _error_key(err)
            else:
                return self.async_update_and_abort(
                    entry,
                    data_updates={
                        CONF_APP_KEY: app_key,
                        CONF_APP_SECRET: app_secret,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(user_input or entry.data),
            errors=errors,
        )


class EzvizIotCnOptionsFlow(config_entries.OptionsFlow):
    """Manage bounded polling, image, webhook, and device-selection options."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show and save one complete options form."""
        errors: dict[str, str] = {}
        devices: tuple[EzvizDevice, ...] = ()
        values = user_input or self.config_entry.options
        hub_name: str | None = None
        if user_input is not None:
            try:
                hub_name = _normalize_hub_name(user_input[CONF_HUB_NAME])
            except ValueError:
                errors[CONF_HUB_NAME] = "invalid_hub_name"
        try:
            devices = await _async_validate_credentials(
                self,
                str(self.config_entry.data[CONF_APP_KEY]),
                str(self.config_entry.data[CONF_APP_SECRET]),
                include_shared_devices=include_shared_devices(values),
            )
        except Exception as err:
            errors["base"] = _error_key(err)

        if user_input is not None and not errors:
            options = dict(user_input)
            options.pop(CONF_HUB_NAME)
            assert hub_name is not None
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                title=hub_name,
                options=options,
            )
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(devices, user_input),
            errors=errors,
            description_placeholders=self._description_placeholders(
                devices,
                user_input,
            ),
        )

    def _description_placeholders(
        self,
        devices: tuple[EzvizDevice, ...],
        user_input: dict[str, Any] | None,
    ) -> dict[str, str]:
        """Build bounded security and API-usage values for the options UI."""
        values = user_input or self.config_entry.options
        selected = effective_device_serials(
            values,
            (device.serial for device in devices),
        )
        capture_channel_count = self._capture_channel_count(selected)
        estimate = estimate_daily_usage(
            include_shared_devices=include_shared_devices(values),
            enable_alarm_polling=bool(values.get(CONF_ENABLE_ALARM_POLLING, True)),
            alarm_interval=_positive_int(
                values.get(CONF_ALARM_INTERVAL),
                DEFAULT_ALARM_INTERVAL,
            ),
            state_interval=_positive_int(
                values.get(CONF_STATE_INTERVAL),
                DEFAULT_STATE_INTERVAL,
            ),
            metadata_interval=_positive_int(
                values.get(CONF_METADATA_INTERVAL),
                DEFAULT_METADATA_INTERVAL,
            ),
            selected_device_count=len(selected),
            enable_scheduled_captures=bool(
                values.get(CONF_ENABLE_SCHEDULED_CAPTURES, False)
            ),
            capture_interval=_positive_int(
                values.get(CONF_CAPTURE_INTERVAL),
                DEFAULT_CAPTURE_INTERVAL,
            ),
            capture_capable_channel_count=capture_channel_count,
            light_capable_device_count=self._light_candidate_count(selected),
        )
        return {
            "webhook_url": self._webhook_url(),
            "estimated_daily_api_calls": str(estimate.total_api_calls),
            "estimated_daily_image_downloads": str(estimate.image_downloads),
            "included_device_count": str(len(selected)),
            "capture_channel_count": str(capture_channel_count),
        }

    def _capture_channel_count(self, selected: set[str]) -> int:
        """Count only channels whose runtime metadata verifies capture support."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "metadata_coordinator", None)
        snapshot = getattr(coordinator, "data", None)
        if not isinstance(snapshot, MetadataSnapshot):
            return 0
        capture_devices = {
            capabilities.device_serial
            for capabilities in snapshot.capabilities
            if capabilities.supports_capture and capabilities.device_serial in selected
        }
        return sum(
            channel.device_serial in capture_devices for channel in snapshot.channels
        )

    def _light_candidate_count(self, selected: set[str]) -> int:
        """Count only directly controlled devices with verified light capability."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "metadata_coordinator", None)
        snapshot = getattr(coordinator, "data", None)
        if not isinstance(snapshot, MetadataSnapshot):
            return 0
        direct = {
            device.serial
            for device in snapshot.devices
            if device.serial in selected and not device.is_shared
        }
        return sum(
            capability.device_serial in direct and capability.supports_alarm_light
            for capability in snapshot.capabilities
        )

    def _webhook_url(self) -> str:
        """Return the current private webhook URL or an ungenerated marker."""
        value = self.config_entry.data.get(CONF_WEBHOOK_ID)
        if not isinstance(value, str) or not value:
            return "-"
        try:
            return ha_webhook.async_generate_url(self.hass, value)
        except HomeAssistantError:
            return ha_webhook.async_generate_path(value)

    def _options_schema(
        self,
        devices: tuple[EzvizDevice, ...],
        user_input: dict[str, Any] | None,
    ) -> vol.Schema:
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HUB_NAME,
                    default=self.config_entry.title or _HUB_NAME_PREFIX,
                ): TextSelector(TextSelectorConfig(autocomplete="off")),
                vol.Optional(
                    CONF_INCLUDE_SHARED_DEVICES,
                    default=include_shared_devices(options),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_ALARM_POLLING,
                    default=options.get(CONF_ENABLE_ALARM_POLLING, True),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ALARM_INTERVAL,
                    default=options.get(CONF_ALARM_INTERVAL, DEFAULT_ALARM_INTERVAL),
                ): _number_selector(MIN_ALARM_INTERVAL, MAX_ALARM_INTERVAL),
                vol.Optional(
                    CONF_STATE_INTERVAL,
                    default=options.get(CONF_STATE_INTERVAL, DEFAULT_STATE_INTERVAL),
                ): _number_selector(MIN_STATE_INTERVAL, MAX_STATE_INTERVAL),
                vol.Optional(
                    CONF_METADATA_INTERVAL,
                    default=options.get(
                        CONF_METADATA_INTERVAL, DEFAULT_METADATA_INTERVAL
                    ),
                ): _number_selector(MIN_METADATA_INTERVAL, MAX_METADATA_INTERVAL),
                vol.Optional(
                    CONF_ENABLE_SCHEDULED_CAPTURES,
                    default=options.get(CONF_ENABLE_SCHEDULED_CAPTURES, False),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_CAPTURE_INTERVAL,
                    default=options.get(
                        CONF_CAPTURE_INTERVAL, DEFAULT_CAPTURE_INTERVAL
                    ),
                ): _number_selector(MIN_CAPTURE_INTERVAL, MAX_CAPTURE_INTERVAL),
                vol.Optional(
                    CONF_ENABLE_ALARM_IMAGES,
                    default=options.get(CONF_ENABLE_ALARM_IMAGES, True),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_MOTION_HOLD_SECONDS,
                    default=options.get(
                        CONF_MOTION_HOLD_SECONDS, DEFAULT_MOTION_HOLD_SECONDS
                    ),
                ): _number_selector(MIN_MOTION_HOLD_SECONDS, MAX_MOTION_HOLD_SECONDS),
                vol.Optional(
                    CONF_ENABLE_NATIVE_WEBHOOK,
                    default=options.get(CONF_ENABLE_NATIVE_WEBHOOK, False),
                ): BooleanSelector(),
                **_device_filter_schema_items(
                    devices,
                    mode=device_filter_mode(options),
                    selected=configured_device_serials(options),
                ),
            }
        )
        if user_input:
            return self.add_suggested_values_to_schema(schema, user_input)
        return schema


def _device_filter_schema(
    devices: tuple[EzvizDevice, ...],
    *,
    mode: str,
    selected: Iterable[str],
) -> vol.Schema:
    return vol.Schema(
        _device_filter_schema_items(devices, mode=mode, selected=selected)
    )


def _device_filter_schema_items(
    devices: tuple[EzvizDevice, ...],
    *,
    mode: str,
    selected: Iterable[str],
) -> dict[vol.Marker, SelectSelector]:
    device_options = [
        SelectOptionDict(
            value=device.serial,
            label=(f"{device.name} ({device.model})" if device.model else device.name),
        )
        for device in devices
    ]
    return {
        vol.Required(CONF_DEVICE_FILTER_MODE, default=mode): SelectSelector(
            SelectSelectorConfig(
                options=[FILTER_MODE_INCLUDE, FILTER_MODE_EXCLUDE],
                translation_key="device_filter_mode",
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(
            CONF_SELECTED_DEVICES,
            default=list(selected),
        ): SelectSelector(
            SelectSelectorConfig(
                options=device_options,
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }


def _number_selector(minimum: int, maximum: int) -> vol.All:
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=minimum,
                max=maximum,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="seconds",
            )
        ),
        vol.Coerce(int),
    )


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return default
    return parsed if parsed > 0 else default
