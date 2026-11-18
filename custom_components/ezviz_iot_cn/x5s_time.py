"""Local ONVIF time-zone control for an optionally configured X5S recorder."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network
from typing import Any, override
from xml.etree import ElementTree as XML
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .runtime import EzvizConfigEntry

CONF_X5S_HOST = "x5s_host"
CONF_X5S_USERNAME = "x5s_username"
CONF_X5S_PASSWORD = "x5s_password"

AUTO = "melbourne_auto"
OPTIONS = {"utc_plus_10": "EST-10", "utc_plus_11": "SBT-11"}
MELBOURNE = ZoneInfo("Australia/Melbourne")
LOCAL_NETWORKS = tuple(
    IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SOAP = "http://www.w3.org/2003/05/soap-envelope"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
SECURITY = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-"


def x5s_host(value: str) -> str:
    """Accept a private LAN IPv4 address and prevent accidental credential redirects."""
    address = IPv4Address(value.strip())
    if not any(address in network for network in LOCAL_NETWORKS):
        raise ValueError("Expected private IPv4 address")
    return str(address)


def melbourne_option(now: datetime) -> str:
    """Return the matching fixed offset, using Australia's regional time rules."""
    return "utc_plus_11" if now.astimezone(MELBOURNE).dst() else "utc_plus_10"


@dataclass(frozen=True)
class RecorderTime:
    """A read-back of the recorder's ONVIF clock and configuration."""

    zone: str
    mode: str
    dst: bool
    utc: datetime
    local: datetime

    @property
    def fixed_option(self) -> str | None:
        """Identify the effective configured offset from local and UTC values."""
        if self.dst:
            return None
        offset = self.local - self.utc.replace(tzinfo=None)
        return next(
            (
                key
                for key, hours in (("utc_plus_10", 10), ("utc_plus_11", 11))
                if offset == timedelta(hours=hours)
            ),
            None,
        )


def _element(parent: XML.Element, name: str, namespace: str = SCHEMA) -> XML.Element:
    return XML.SubElement(parent, f"{{{namespace}}}{name}")


class X5SClient:
    """Send only clock-related ONVIF requests to the selected recorder."""

    def __init__(
        self, hass: HomeAssistant, host: str, username: str, password: str
    ) -> None:
        self._http = async_get_clientsession(hass)
        self._url = f"http://{x5s_host(host)}/onvif/device_service"
        self._username = username
        self._password = password

    def _security(self, envelope: XML.Element) -> None:
        nonce = secrets.token_bytes(20)
        created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        wsse = SECURITY + "wssecurity-secext-1.0.xsd"
        wsu = SECURITY + "wssecurity-utility-1.0.xsd"
        header = _element(envelope, "Header", SOAP)
        security = _element(header, "Security", wsse)
        security.set(f"{{{SOAP}}}mustUnderstand", "1")
        token = _element(security, "UsernameToken", wsse)
        _element(token, "Username", wsse).text = self._username
        digest = _element(token, "Password", wsse)
        digest.set("Type", SECURITY + "username-token-profile-1.0#PasswordDigest")
        digest.text = base64.b64encode(
            hashlib.sha1(
                nonce + created.encode() + self._password.encode(),
                usedforsecurity=False,
            ).digest()
        ).decode()
        nonce_node = _element(token, "Nonce", wsse)
        nonce_node.set(
            "EncodingType", SECURITY + "soap-message-security-1.0#Base64Binary"
        )
        nonce_node.text = base64.b64encode(nonce).decode()
        _element(token, "Created", wsu).text = created

    async def _request(
        self, name: str, *, authenticate: bool = False, zone: str | None = None
    ) -> XML.Element:
        envelope = XML.Element(f"{{{SOAP}}}Envelope")
        if authenticate:
            self._security(envelope)
        action = _element(_element(envelope, "Body", SOAP), name, DEVICE)
        if zone is not None:
            _element(action, "DateTimeType", DEVICE).text = "NTP"
            _element(action, "DaylightSavings", DEVICE).text = "false"
            _element(_element(action, "TimeZone", DEVICE), "TZ").text = zone
        content_type = f'application/soap+xml; charset=utf-8; action="{DEVICE}/{name}"'
        try:
            async with self._http.post(
                self._url,
                data=XML.tostring(envelope, encoding="utf-8"),
                headers={"Content-Type": content_type},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=7),
            ) as response:
                raw = await response.content.readexactly(65_537)
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HomeAssistantError("X5S ONVIF connection failed") from exc
        if len(raw) > 65_536:
            raise HomeAssistantError("X5S ONVIF response is too large")
        try:
            root = XML.fromstring(raw)
        except XML.ParseError:
            raise HomeAssistantError("X5S returned invalid ONVIF XML") from None
        if root.find(f".//{{{DEVICE}}}{name}Response") is None:
            codes = [
                item.text
                for item in root.findall(f".//{{{SOAP}}}Value")
                if item.text and re.fullmatch(r"[a-zA-Z0-9_.:-]+", item.text)
            ]
            raise HomeAssistantError(
                f"X5S rejected {name} ({', '.join(codes) or 'unknown fault'})"
            )
        return root

    async def validate(self) -> None:
        """Verify the credentials using an authenticated read."""
        await self._request("GetNTP", authenticate=True)
        await self.read_time()

    async def read_time(self) -> RecorderTime:
        """Read current UTC, local time, time zone, and NTP mode."""
        root = await self._request("GetSystemDateAndTime")

        def date(tag: str, *, utc: bool = False) -> datetime:
            node = root.find(f".//{{{SCHEMA}}}{tag}")
            if node is None:
                raise HomeAssistantError(f"X5S did not report {tag}")
            try:
                parts = [
                    int(node.findtext(f".//{{{SCHEMA}}}{field}", ""))
                    for field in ("Year", "Month", "Day", "Hour", "Minute", "Second")
                ]
                year, month, day, hour, minute, second = parts
                return datetime(
                    year, month, day, hour, minute, second, tzinfo=UTC if utc else None
                )
            except ValueError:
                raise HomeAssistantError(f"X5S returned invalid {tag}") from None

        dst = root.findtext(f".//{{{SCHEMA}}}DaylightSavings", "").strip()
        if dst not in {"0", "1", "false", "true"}:
            raise HomeAssistantError("X5S did not report its daylight-saving state")
        return RecorderTime(
            root.findtext(f".//{{{SCHEMA}}}TZ", ""),
            root.findtext(f".//{{{SCHEMA}}}DateTimeType", ""),
            dst in {"true", "1"},
            date("UTCDateTime", utc=True),
            date("LocalDateTime"),
        )

    async def set_offset(self, option: str, current: RecorderTime) -> RecorderTime:
        """Make one fixed-zone write and verify the resulting recorder state."""
        if current.mode != "NTP" or current.dst:
            raise HomeAssistantError(
                "X5S must use NTP with firmware daylight saving disabled"
            )
        if current.fixed_option == option:
            return current
        failure = None
        try:
            await self._request(
                "SetSystemDateAndTime", authenticate=True, zone=OPTIONS[option]
            )
        except HomeAssistantError as exc:
            failure = exc
        updated = await self.read_time()
        if updated.fixed_option != option or updated.mode != "NTP" or updated.dst:
            raise HomeAssistantError(
                "X5S fixed time-zone change could not be confirmed"
            ) from failure
        return updated


class X5STimeSelect(SelectEntity, RestoreEntity):
    """Allow an administrator to choose Melbourne scheduling or a fixed offset."""

    entity_description = SelectEntityDescription(
        key="x5s_time_zone", translation_key="x5s_time_zone"
    )
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, hass: HomeAssistant, entry: EzvizConfigEntry) -> None:
        self._attr_options = [AUTO, *OPTIONS]
        host = entry.options[CONF_X5S_HOST]
        self._client = X5SClient(
            hass,
            host,
            entry.options[CONF_X5S_USERNAME],
            entry.options[CONF_X5S_PASSWORD],
        )
        self._attr_unique_id = f"{entry.entry_id}_x5s_{host}_time_zone"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_x5s_{host}")},
            manufacturer="EZVIZ",
            name="X5S",
            model="X5S",
        )
        self._automatic = False
        self._lock = asyncio.Lock()
        self._recorder: RecorderTime | None = None

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        self._automatic = previous is not None and (
            previous.state == AUTO or previous.attributes.get("melbourne_auto") is True
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._check, timedelta(minutes=5))
        )
        await self._check(datetime.now(UTC))

    def _publish(self) -> None:
        current = self._recorder
        self._attr_available = current is not None
        self._attr_current_option = (
            AUTO if self._automatic else current.fixed_option if current else None
        )
        attributes: dict[str, Any] = {"melbourne_auto": self._automatic}
        if current:
            now = datetime.now(UTC)
            melbourne = now.astimezone(MELBOURNE)
            attributes.update(
                {
                    "device_time_zone": current.zone,
                    "device_local_time": current.local.isoformat(),
                    "device_utc_time": current.utc.isoformat(),
                    "ntp_enabled": current.mode == "NTP",
                    "utc_drift_seconds": round((current.utc - now).total_seconds()),
                    "melbourne_drift_seconds": round(
                        (current.local - melbourne.replace(tzinfo=None)).total_seconds()
                    ),
                    "melbourne_fixed_offset": melbourne_option(now),
                }
            )
        self._attr_extra_state_attributes = attributes
        self.async_write_ha_state()

    async def _check(self, now: datetime) -> None:
        async with self._lock:
            try:
                self._recorder = await self._client.read_time()
                if self._automatic:
                    self._recorder = await self._client.set_offset(
                        melbourne_option(now), self._recorder
                    )
            except HomeAssistantError:
                self._recorder = None
            self._publish()

    @override
    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError("Unknown X5S time-zone option")
        async with self._lock:
            self._recorder = await self._client.read_time()
            target = melbourne_option(datetime.now(UTC)) if option == AUTO else option
            self._recorder = await self._client.set_offset(target, self._recorder)
            self._automatic = option == AUTO
            self._publish()
