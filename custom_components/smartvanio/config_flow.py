"""Config flow for SmartVan.io integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_MQTT_PREFIX,
    DEFAULT_MQTT_PREFIX,
    CONF_BETA_CHANNEL,
    DEFAULT_BETA_CHANNEL,
    BLE_SERVICE_UUID,
    BLE_MQTT_CONFIG_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)

# Plain text fields — password managers were autofilling generated passwords when type=PASSWORD
# was used, even with autocomplete="new-password"/"one-time-code". User is already authenticated
# to HA, so dot-masking provides no security benefit.
WIFI_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.TEXT,
        autocomplete="off",
    )
)
MQTT_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.TEXT,
        autocomplete="off",
    )
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MQTT_PREFIX, default=DEFAULT_MQTT_PREFIX): str,
        vol.Optional(CONF_BETA_CHANNEL, default=DEFAULT_BETA_CHANNEL): bool,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input.

    Check that MQTT is available and the prefix is valid.
    """
    # Verify MQTT integration is loaded
    if "mqtt" not in hass.config.components:
        raise MqttNotAvailable

    prefix = data.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
    if not prefix or "/" in prefix or "#" in prefix or "+" in prefix:
        raise InvalidPrefix

    return {"title": f"SmartVan.io ({prefix})"}


class SmartVanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SmartVan.io."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return SmartVanOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialise flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._zeroconf_info: ZeroconfServiceInfo | None = None
        self._device_name: str | None = None
        self._device_host: str | None = None
        self._device_on_wifi: bool = False
        self._pending_wifi_ssid: str | None = None

        # Legacy firmware rescue flash state
        self._flash_host: str | None = None
        self._flash_firmware_type: str | None = None
        self._flash_branch: str = "beta"
        self._flash_task: asyncio.Task | None = None
        self._flash_error: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user adds the integration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except MqttNotAvailable:
                errors["base"] = "mqtt_not_available"
            except InvalidPrefix:
                errors["base"] = "invalid_prefix"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Prevent duplicate entries
                await self.async_set_unique_id(
                    user_input.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ── Zeroconf (mDNS/WiFi) discovery ──────────────────────────

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle device discovered via mDNS on the local network."""
        name = discovery_info.name.split(".")[0]
        host = discovery_info.host
        _LOGGER.info(
            "SmartVan.io device discovered via mDNS: %s (%s)", name, host,
        )

        # Deduplicate by hostname
        await self.async_set_unique_id(name)
        self._abort_if_unique_id_configured()

        self._zeroconf_info = discovery_info
        self._device_name = name
        self._device_host = host
        self._device_on_wifi = True

        # Check if device is already on MQTT — fully provisioned
        if await self._check_device_on_mqtt_by_name(name):
            _LOGGER.info("Device %s already on MQTT, skipping provisioning", name)
            return self._create_or_update_entry()

        # Legacy firmware has no /provision endpoint — offer rescue flash instead
        # of the normal MQTT provisioning form.
        if not await self._device_supports_provisioning(host):
            _LOGGER.info(
                "Device %s (%s) looks like legacy firmware — routing to rescue flash",
                name, host,
            )
            self._flash_host = host
            self._flash_firmware_type = _guess_firmware_type(name)
            self._flash_branch = (
                "beta"
                if self._get_existing_beta_channel()
                else "main"
            )
            self.context["title_placeholders"] = {"name": name}
            return await self.async_step_flash_legacy()

        # Device on WiFi but not MQTT — show MQTT credentials form
        self.context["title_placeholders"] = {"name": name}
        return await self._show_zeroconf_mqtt_form()

    def _get_existing_beta_channel(self) -> bool:
        """Read beta-channel preference from the existing hub entry, if any."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            return entry.data.get(CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL)
        return DEFAULT_BETA_CHANNEL

    # ── Legacy rescue flash ─────────────────────────────────────

    async def async_step_flash_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm host (firmware type is auto-derived from the hostname).

        Asking the user to pick a firmware type was both a security
        footgun (wrong choice bricks the device) and a UX failure for a
        flow that already knows what kind of device it's talking to.
        If we can't guess, abort cleanly rather than guess wrong.
        """
        if not self._flash_firmware_type:
            return self.async_abort(
                reason="unknown_firmware_type",
                description_placeholders={"name": self._device_name or "device"},
            )

        if user_input is None:
            return self.async_show_form(
                step_id="flash_legacy",
                data_schema=vol.Schema(
                    {vol.Required("host", default=self._flash_host or ""): str}
                ),
                description_placeholders={
                    "name": self._device_name or "",
                    "firmware_type": self._flash_firmware_type,
                },
            )

        self._flash_host = user_input["host"].strip()
        self._flash_task = None
        self._flash_error = None
        return await self.async_step_flash_progress()

    async def async_step_flash_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._flash_task is None:
            self._flash_task = self.hass.async_create_task(
                _run_legacy_flash(
                    self.hass,
                    self._flash_host,
                    self._flash_firmware_type,
                    self._flash_branch,
                )
            )

        if not self._flash_task.done():
            return self.async_show_progress(
                step_id="flash_progress",
                progress_action="flashing",
                progress_task=self._flash_task,
            )

        err = self._flash_task.exception()
        if err is not None:
            self._flash_error = str(err)
            return self.async_show_progress_done(next_step_id="flash_failure")
        return self.async_show_progress_done(next_step_id="flash_success")

    async def async_step_flash_success(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="flash_success")

    async def async_step_flash_failure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="flash_failed",
            description_placeholders={"error": self._flash_error or "unknown"},
        )

    async def _device_supports_provisioning(self, host: str) -> bool:
        """Probe the device's HTTP /provision endpoint.

        New firmware's /provision accepts POST; a GET typically returns 200,
        204, 400 (bad request) or 405 (method not allowed). Legacy firmware
        without the endpoint returns 404, or ESPHome's default web_server
        can return 500 for unhandled routes. Treat anything outside the known
        new-firmware response set as legacy.
        """
        url = f"http://{host}/provision"
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=5, allow_redirects=False) as resp:
                return resp.status in (200, 204, 400, 405)
        except Exception:  # noqa: BLE001
            return False

    async def _show_zeroconf_mqtt_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show form with MQTT fields for a WiFi-discovered device."""
        mqtt_creds = self._get_mqtt_credentials()
        default_broker = mqtt_creds["broker"]
        if default_broker in ("core-mosquitto", "localhost", "127.0.0.1", "mosquitto"):
            default_broker = await self._resolve_host_ip() or default_broker

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("mqtt_broker", default=default_broker): str,
                    vol.Optional(
                        "mqtt_username", default=mqtt_creds["username"]
                    ): str,
                    vol.Optional(
                        "mqtt_password", default=mqtt_creds["password"]
                    ): MQTT_PASSWORD_SELECTOR,
                }
            ),
            errors=errors or {},
            description_placeholders={"name": self._device_name},
        )

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MQTT credential submission for mDNS-discovered device."""
        if user_input is None:
            return await self._show_zeroconf_mqtt_form()

        broker = user_input.get("mqtt_broker", "").strip()
        username = user_input.get("mqtt_username", "").strip()
        password = user_input.get("mqtt_password", "")

        if not broker:
            return await self._show_zeroconf_mqtt_form(
                errors={"mqtt_broker": "broker_required"}
            )

        # Send MQTT credentials via HTTP POST to the device
        success = await self._provision_via_http(
            self._device_host, broker, username, password
        )
        if not success:
            return await self._show_zeroconf_mqtt_form(
                errors={"base": "http_provision_failed"}
            )

        return await self.async_step_await_mqtt()

    async def _provision_via_http(
        self, host: str, broker: str, username: str, password: str
    ) -> bool:
        """Send MQTT credentials to the device via HTTP POST (form-encoded)."""
        url = f"http://{host}/provision"
        form_data = {
            "broker": broker,
            "username": username,
            "password": password,
        }
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, data=form_data, timeout=10) as resp:
                if resp.status == 200:
                    _LOGGER.info("MQTT credentials sent to %s via HTTP", host)
                    return True
                _LOGGER.error(
                    "HTTP provision failed: %s %s", resp.status, await resp.text()
                )
                return False
        except Exception:
            _LOGGER.exception("Failed to send MQTT config to %s via HTTP", host)
            return False

    async def _check_device_on_mqtt_by_name(self, device_name: str) -> bool:
        """Check if a device with the given name is already publishing on MQTT."""
        if "mqtt" not in self.hass.config.components:
            return False

        from homeassistant.components.mqtt import async_subscribe

        found = asyncio.Event()

        def _on_message(msg):
            try:
                payload = json.loads(msg.payload)
                if payload.get("device_id") == device_name:
                    found.set()
            except (json.JSONDecodeError, AttributeError):
                pass

        unsub = await async_subscribe(
            self.hass, f"{DEFAULT_MQTT_PREFIX}/+/config", _on_message, qos=0
        )
        try:
            await asyncio.wait_for(found.wait(), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            unsub()

    # ── Bluetooth discovery ─────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle device discovered via Bluetooth."""
        _LOGGER.info(
            "SmartVan.io BLE device discovered: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )

        # Deduplicate by BLE address
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._device_name = discovery_info.name or "SmartVan.io Device"

        # Show the discovery confirmation to the user
        self.context["title_placeholders"] = {"name": self._device_name}
        return await self.async_step_bluetooth_confirm()

    def _get_mqtt_credentials(self) -> dict[str, str]:
        """Read broker/username/password from the existing MQTT config entry."""
        for entry in self.hass.config_entries.async_entries("mqtt"):
            data = entry.data or {}
            return {
                "broker": data.get("broker", ""),
                "username": data.get("username", ""),
                "password": data.get("password", ""),
            }
        return {"broker": "", "username": "", "password": ""}

    def _ble_mac_to_wifi_suffix(self) -> str:
        """Derive the WiFi MAC suffix from the BLE MAC address.

        ESP32 BLE MAC = WiFi MAC + 2 (last octet).
        Returns the last 3 bytes of the WiFi MAC as a lowercase hex string
        (e.g. '77a5a0'), which appears in mDNS hostnames.
        """
        ble_mac = self._discovery_info.address.upper().replace("-", ":")
        parts = ble_mac.split(":")
        last = int(parts[-1], 16) - 2
        if last < 0:
            last += 256
        return (parts[-3] + parts[-2] + f"{last:02X}").lower()

    async def _check_device_on_network(self) -> bool:
        """Check if the device is reachable on the local network via mDNS."""
        if not self._discovery_info:
            return False

        mac_suffix = self._ble_mac_to_wifi_suffix()

        def _blocking_mdns_check() -> bool:
            from zeroconf import Zeroconf, ServiceBrowser
            import threading

            found = threading.Event()

            class Listener:
                def add_service(self, zc, stype, name):
                    if mac_suffix in name.lower():
                        found.set()

                def remove_service(self, zc, stype, name):
                    pass

                def update_service(self, zc, stype, name):
                    pass

            zc = Zeroconf()
            browser = ServiceBrowser(zc, "_smartvaniolib._tcp.local.", Listener())
            try:
                return found.wait(timeout=3.0)
            finally:
                browser.cancel()
                zc.close()

        try:
            return await self.hass.async_add_executor_job(_blocking_mdns_check)
        except Exception:
            _LOGGER.debug("mDNS check failed, assuming device not on network")
            return False

    async def _check_device_on_mqtt(self) -> bool:
        """Check if the discovered BLE device is already online via MQTT."""
        if "mqtt" not in self.hass.config.components or not self._discovery_info:
            return False

        from homeassistant.components.mqtt import async_subscribe

        ble_mac = self._discovery_info.address.upper().replace("-", ":")
        # ESP32 BLE MAC = WiFi MAC + 2 (last octet)
        # Convert BLE MAC to expected WiFi MAC for matching
        mac_parts = ble_mac.split(":")
        wifi_last_octet = int(mac_parts[-1], 16) - 2
        if wifi_last_octet < 0:
            wifi_last_octet += 256
        expected_wifi_mac = ":".join(mac_parts[:-1] + [f"{wifi_last_octet:02X}"])

        found = asyncio.Event()

        def _on_message(msg):
            try:
                payload = json.loads(msg.payload)
                device_mac = payload.get("mac", "").upper().replace("-", ":")
                if device_mac == expected_wifi_mac:
                    found.set()
            except (json.JSONDecodeError, AttributeError):
                pass

        unsub = await async_subscribe(
            self.hass, f"{DEFAULT_MQTT_PREFIX}/+/config", _on_message, qos=0
        )
        try:
            # Wait up to 5s for a matching config message (published every 30s,
            # but retained messages arrive immediately)
            await asyncio.wait_for(found.wait(), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            unsub()

    def _create_or_update_entry(
        self, extra_data: dict | None = None
    ) -> ConfigFlowResult:
        """Create a new hub entry or update the existing one."""
        entry_data = {CONF_MQTT_PREFIX: DEFAULT_MQTT_PREFIX}
        if extra_data:
            entry_data.update(extra_data)

        existing = self.hass.config_entries.async_entries(DOMAIN)
        if existing:
            self.hass.config_entries.async_update_entry(
                existing[0], data={**existing[0].data, **entry_data}
            )
            return self.async_abort(reason="device_provisioned")

        return self.async_create_entry(
            title="SmartVan.io", data=entry_data
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm BLE device — detect provisioning state and show appropriate form."""
        if user_input is not None:
            # Form was submitted — delegate to the appropriate handler
            if self._device_on_wifi:
                return await self._handle_mqtt_only_submit(user_input)
            return await self._handle_full_submit(user_input)

        # Tier 1: device already on MQTT — fully provisioned
        if await self._check_device_on_mqtt():
            _LOGGER.info(
                "Device %s already on MQTT, skipping provisioning",
                self._device_name,
            )
            return self._create_or_update_entry()

        # Tier 2: device on WiFi but not MQTT — only need MQTT credentials
        if await self._check_device_on_network():
            _LOGGER.info(
                "Device %s on WiFi but not MQTT, requesting MQTT credentials",
                self._device_name,
            )
            self._device_on_wifi = True
            return await self._show_mqtt_only_form()

        # Tier 3: device not on network — need everything
        _LOGGER.info(
            "Device %s not on network, requesting full credentials",
            self._device_name,
        )
        return await self._show_full_form()

    async def _show_mqtt_only_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show form with only MQTT fields (device already on WiFi)."""
        mqtt_creds = self._get_mqtt_credentials()
        default_broker = mqtt_creds["broker"]
        if default_broker in ("core-mosquitto", "localhost", "127.0.0.1"):
            default_broker = await self._resolve_host_ip() or default_broker

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("mqtt_broker", default=default_broker): str,
                    vol.Optional(
                        "mqtt_username", default=mqtt_creds["username"]
                    ): str,
                    vol.Optional(
                        "mqtt_password", default=mqtt_creds["password"]
                    ): MQTT_PASSWORD_SELECTOR,
                }
            ),
            errors=errors or {},
            description_placeholders={"name": self._device_name},
        )

    async def _show_full_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show form with WiFi + MQTT fields (device needs full provisioning)."""
        default_ssid = ""
        existing = self.hass.config_entries.async_entries(DOMAIN)
        if existing:
            saved_ssid = existing[0].data.get("wifi_ssid", "")
            if saved_ssid:
                default_ssid = saved_ssid

        mqtt_creds = self._get_mqtt_credentials()
        default_broker = mqtt_creds["broker"]
        if default_broker in ("core-mosquitto", "localhost", "127.0.0.1"):
            default_broker = await self._resolve_host_ip() or default_broker

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("wifi_ssid", default=default_ssid): str,
                    vol.Optional("wifi_password", default=""): WIFI_PASSWORD_SELECTOR,
                    vol.Required("mqtt_broker", default=default_broker): str,
                    vol.Optional(
                        "mqtt_username", default=mqtt_creds["username"]
                    ): str,
                    vol.Optional(
                        "mqtt_password", default=mqtt_creds["password"]
                    ): MQTT_PASSWORD_SELECTOR,
                }
            ),
            errors=errors or {},
            description_placeholders={"name": self._device_name},
        )

    async def _handle_mqtt_only_submit(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle submission when device is on WiFi — send only MQTT creds."""
        broker = user_input.get("mqtt_broker", "").strip()
        username = user_input.get("mqtt_username", "").strip()
        password = user_input.get("mqtt_password", "")

        if not broker:
            return await self._show_mqtt_only_form(
                errors={"mqtt_broker": "broker_required"}
            )

        # Send MQTT credentials via BLE (WiFi fields empty — device keeps existing)
        success = await self._provision_via_ble("", "", broker, username, password)
        if not success:
            return await self._show_mqtt_only_form(
                errors={"base": "ble_write_failed"}
            )

        self._pending_wifi_ssid = None
        return await self.async_step_await_mqtt()

    async def _handle_full_submit(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle submission when device needs full provisioning."""
        wifi_ssid = user_input.get("wifi_ssid", "").strip()
        wifi_password = user_input.get("wifi_password", "")
        broker = user_input.get("mqtt_broker", "").strip()
        username = user_input.get("mqtt_username", "").strip()
        password = user_input.get("mqtt_password", "")

        if not wifi_ssid:
            return await self._show_full_form(
                errors={"wifi_ssid": "ssid_required"}
            )
        if not broker:
            return await self._show_full_form(
                errors={"mqtt_broker": "broker_required"}
            )

        success = await self._provision_via_ble(
            wifi_ssid, wifi_password, broker, username, password
        )
        if not success:
            return await self._show_full_form(
                errors={"base": "ble_write_failed"}
            )

        self._pending_wifi_ssid = wifi_ssid
        return await self.async_step_await_mqtt()

    async def async_step_await_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the device to appear on MQTT after BLE provisioning."""
        if user_input is not None:
            # User clicked retry — go back to the appropriate form
            if self._device_on_wifi:
                return await self._show_mqtt_only_form()
            return await self._show_full_form()

        # Wait for MQTT confirmation (device reboots + connects, ~10-30s)
        device_online = await self._wait_for_mqtt_confirmation(timeout=60)

        if device_online:
            extra = {}
            if self._pending_wifi_ssid:
                extra["wifi_ssid"] = self._pending_wifi_ssid
            return self._create_or_update_entry(extra or None)

        # Device didn't appear — show error with retry
        return self.async_show_form(
            step_id="await_mqtt",
            data_schema=vol.Schema({}),
            errors={"base": "device_not_responding"},
            description_placeholders={"name": self._device_name},
        )

    async def _wait_for_mqtt_confirmation(self, timeout: int = 60) -> bool:
        """Subscribe to MQTT and wait for the device's config message."""
        if "mqtt" not in self.hass.config.components or not self._discovery_info:
            return False

        from homeassistant.components.mqtt import async_subscribe

        ble_mac = self._discovery_info.address.upper().replace("-", ":")
        mac_parts = ble_mac.split(":")
        wifi_last_octet = int(mac_parts[-1], 16) - 2
        if wifi_last_octet < 0:
            wifi_last_octet += 256
        expected_wifi_mac = ":".join(mac_parts[:-1] + [f"{wifi_last_octet:02X}"])

        found = asyncio.Event()

        def _on_message(msg):
            try:
                payload = json.loads(msg.payload)
                device_mac = payload.get("mac", "").upper().replace("-", ":")
                if device_mac == expected_wifi_mac:
                    found.set()
            except (json.JSONDecodeError, AttributeError):
                pass

        unsub = await async_subscribe(
            self.hass, f"{DEFAULT_MQTT_PREFIX}/+/config", _on_message, qos=0
        )
        try:
            await asyncio.wait_for(found.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            unsub()

    async def _resolve_host_ip(self) -> str | None:
        """Get the HA host's LAN IP address."""
        try:
            from homeassistant.helpers.network import get_url
            from urllib.parse import urlparse
            ha_url = get_url(self.hass, prefer_external=False)
            host = urlparse(ha_url).hostname
            if host and host not in ("localhost", "127.0.0.1"):
                return host
        except Exception:
            pass
        # Fallback: resolve via socket
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            pass
        return None

    async def _provision_via_ble(
        self,
        wifi_ssid: str,
        wifi_password: str,
        broker: str,
        username: str,
        password: str,
    ) -> bool:
        """Write WiFi + MQTT credentials to the device over BLE GATT."""
        if self._discovery_info is None:
            return False

        from bleak import BleakClient

        payload = json.dumps(
            {
                "ssid": wifi_ssid,
                "wifi_password": wifi_password,
                "broker": broker,
                "username": username,
                "password": password,
            },
            separators=(",", ":"),
        ).encode("utf-8")

        try:
            async with BleakClient(self._discovery_info.device) as client:
                await client.write_gatt_char(
                    BLE_MQTT_CONFIG_CHAR_UUID, payload, response=True
                )
                _LOGGER.info(
                    "MQTT credentials sent to %s via BLE",
                    self._discovery_info.address,
                )
                return True
        except Exception:
            _LOGGER.exception(
                "Failed to write MQTT config to %s",
                self._discovery_info.address,
            )
            return False


FIRMWARE_TYPES = [
    "inclinometer",
    "resistive_sensor",
    "led",
    "gledopto_din",
    "truma",
    "sonoff_m5_3g",
]

# Hostname keyword → firmware_type guess for legacy zeroconf devices.
# "led" is ambiguous (LED.yaml vs LED_GLEDOPTO_DIN.yaml) — user must confirm.
_NAME_TO_FIRMWARE_TYPE = {
    "res": "resistive_sensor",
    "inclinometer": "inclinometer",
    "truma": "truma",
    "switch": "sonoff_m5_3g",
    "led": "led",
}


def _guess_firmware_type(device_name: str | None) -> str | None:
    if not device_name:
        return None
    lowered = device_name.lower()
    for kw, fw_type in _NAME_TO_FIRMWARE_TYPE.items():
        if kw in lowered:
            return fw_type
    return None


def _build_bin_url(firmware_type: str, branch: str, filename: str) -> str:
    return (
        f"https://raw.githubusercontent.com/Smartvan-io/"
        f"{firmware_type}.bin/refs/heads/{branch}/{filename}"
    )


async def _discover_legacy_candidates(
    hass: HomeAssistant,
) -> list[dict[str, str]]:
    """Browse mDNS for `_smartvaniolib._tcp` and return device candidates.

    Each candidate is {name, host, firmware_type}. Firmware type is derived
    from the hostname, falling back to None if it can't be guessed.
    """

    def _blocking_browse() -> list[dict[str, str]]:
        from zeroconf import ServiceBrowser, Zeroconf
        import socket
        import threading

        results: dict[str, dict[str, str]] = {}
        done = threading.Event()

        class Listener:
            def add_service(self, zc: "Zeroconf", stype: str, name: str) -> None:
                try:
                    info = zc.get_service_info(stype, name, timeout=2000)
                except Exception:
                    return
                if not info:
                    return
                host = None
                for addr in info.addresses or []:
                    try:
                        host = socket.inet_ntoa(addr)
                        break
                    except OSError:
                        continue
                if not host:
                    return
                short_name = name.split(".")[0]
                results[host] = {
                    "name": short_name,
                    "host": host,
                    "firmware_type": _guess_firmware_type(short_name) or "",
                }

            def remove_service(self, zc, stype, name): pass
            def update_service(self, zc, stype, name): pass

        zc = Zeroconf()
        browser = ServiceBrowser(zc, "_smartvaniolib._tcp.local.", Listener())
        try:
            done.wait(timeout=3.5)
        finally:
            browser.cancel()
            zc.close()

        return sorted(results.values(), key=lambda x: x["name"])

    try:
        return await hass.async_add_executor_job(_blocking_browse)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("mDNS browse for legacy candidates failed")
        return []


async def _run_legacy_flash(
    hass: HomeAssistant, host: str, firmware_type: str, branch: str
) -> None:
    """Download firmware from GitHub and push via native ESPHome OTA."""
    from .ota_native import run_ota as run_native_ota

    fw_url = _build_bin_url(firmware_type, branch, "firmware.bin")
    _LOGGER.info(
        "Legacy rescue flash: downloading %s for %s → %s", fw_url, firmware_type, host
    )

    session = async_get_clientsession(hass)
    async with session.get(fw_url, timeout=120) as resp:
        if resp.status != 200:
            raise Exception(
                f"Firmware download failed: HTTP {resp.status} for {fw_url}"
            )
        binary = await resp.read()

    _LOGGER.info(
        "Legacy rescue flash: pushing %d bytes to %s via native OTA", len(binary), host
    )
    await run_native_ota(host, binary)
    _LOGGER.info("Legacy rescue flash: completed successfully for %s", host)


class SmartVanOptionsFlow(OptionsFlow):
    """Handle SmartVan.io options (settings + legacy device rescue flash)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._flash_host: str | None = None
        self._flash_firmware_type: str | None = None
        self._flash_branch: str = "beta"
        self._flash_task: asyncio.Task | None = None
        self._flash_error: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "flash_legacy"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current_beta = self._config_entry.data.get(
            CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_BETA_CHANNEL, default=current_beta): bool,
                }
            ),
        )

    async def async_step_flash_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Auto-discover SmartVan.io devices and let the user pick one to flash.

        The integration does the mDNS browse, derives the firmware type from
        the hostname, and uses the current beta/main channel setting. The only
        decision the user makes is which device to flash.
        """
        self._flash_branch = (
            "beta"
            if self._config_entry.data.get(CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL)
            else "main"
        )

        candidates = await _discover_legacy_candidates(self.hass)
        usable = [c for c in candidates if c["firmware_type"]]

        if user_input is not None:
            picked = user_input["device"]
            for c in candidates:
                if f"{c['host']}|{c['firmware_type']}" == picked:
                    self._flash_host = c["host"]
                    self._flash_firmware_type = c["firmware_type"]
                    self._flash_task = None
                    self._flash_error = None
                    return await self.async_step_flash_progress()
            return self.async_abort(reason="flash_failed",
                                    description_placeholders={"error": "device vanished"})

        if not usable:
            return self.async_abort(reason="no_legacy_devices_found")

        options = {
            f"{c['host']}|{c['firmware_type']}":
                f"{c['name']} — {c['host']} ({c['firmware_type']})"
            for c in usable
        }
        return self.async_show_form(
            step_id="flash_legacy",
            data_schema=vol.Schema(
                {vol.Required("device"): vol.In(options)}
            ),
            description_placeholders={"count": str(len(usable)), "channel": self._flash_branch},
        )

    async def async_step_flash_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show progress while the native OTA runs."""
        if self._flash_task is None:
            self._flash_task = self.hass.async_create_task(
                self._do_legacy_flash()
            )

        if not self._flash_task.done():
            return self.async_show_progress(
                step_id="flash_progress",
                progress_action="flashing",
                progress_task=self._flash_task,
            )

        err = self._flash_task.exception()
        if err is not None:
            self._flash_error = str(err)
            return self.async_show_progress_done(next_step_id="flash_failure")
        return self.async_show_progress_done(next_step_id="flash_success")

    async def async_step_flash_success(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="flash_success")

    async def async_step_flash_failure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="flash_failed",
            description_placeholders={"error": self._flash_error or "unknown"},
        )

    async def _do_legacy_flash(self) -> None:
        await _run_legacy_flash(
            self.hass,
            self._flash_host,
            self._flash_firmware_type,
            self._flash_branch,
        )


class MqttNotAvailable(HomeAssistantError):
    """Error to indicate MQTT is not configured."""


class InvalidPrefix(HomeAssistantError):
    """Error to indicate the MQTT prefix is invalid."""
