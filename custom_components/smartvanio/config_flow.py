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

        # Device on WiFi but not MQTT — show MQTT credentials form
        self.context["title_placeholders"] = {"name": name}
        return await self._show_zeroconf_mqtt_form()

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
                    ): str,
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
                    ): str,
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
                    vol.Optional("wifi_password", default=""): str,
                    vol.Required("mqtt_broker", default=default_broker): str,
                    vol.Optional(
                        "mqtt_username", default=mqtt_creds["username"]
                    ): str,
                    vol.Optional(
                        "mqtt_password", default=mqtt_creds["password"]
                    ): str,
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


class SmartVanOptionsFlow(OptionsFlow):
    """Handle SmartVan.io options (beta channel toggle)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Merge into the main config entry data so update.py can read it
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current_beta = self._config_entry.data.get(
            CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_BETA_CHANNEL, default=current_beta): bool,
                }
            ),
        )


class MqttNotAvailable(HomeAssistantError):
    """Error to indicate MQTT is not configured."""


class InvalidPrefix(HomeAssistantError):
    """Error to indicate the MQTT prefix is invalid."""
