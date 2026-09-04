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
    STATUS_TOPIC_SUFFIX,
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
        # Post-flash: polls the device's new /provision endpoint until
        # it comes back from reboot, then we hand off to the existing
        # zeroconf MQTT form so the user provisions in one continuous
        # flow instead of being aborted out.
        self._post_flash_task: asyncio.Task | None = None

        # MQTT creds the user typed before we discovered the device was
        # legacy. Replayed automatically once the post-flash device
        # exposes /provision, so the user doesn't re-enter them.
        self._pending_mqtt_creds: dict[str, str] | None = None

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

        # If the integration already has a live record of this device
        # (it's published its config payload to MQTT and the hub picked
        # it up), don't show a fresh "Discovered" card. Re-broadcasts of
        # mDNS are normal and shouldn't pester the user.
        if _is_device_adopted(self.hass, name):
            _LOGGER.info("Device %s already adopted, dismissing rediscovery", name)
            return self.async_abort(reason="already_configured")

        self._zeroconf_info = discovery_info
        self._device_name = name
        self._device_host = host
        self._device_on_wifi = True

        # Check if device is already on MQTT — fully provisioned
        if await self._check_device_on_mqtt_by_name(name):
            _LOGGER.info("Device %s already on MQTT, skipping provisioning", name)
            return self._create_or_update_entry()

        # Device is on WiFi (we discovered it via mDNS) but isn't talking
        # to MQTT yet. Show the credential form. The earlier /provision
        # presence probe was the wrong gate: only LED firmware registers
        # the HTTP /provision handler, so resistive/inclinometer/etc. used
        # to get falsely flagged as 'legacy' and routed straight to a
        # re-flash before the user could enter their broker. Remember the
        # firmware-type guess + the host so the user can still flash from
        # an error state if /provision genuinely isn't there.
        self._flash_host = host
        self._flash_firmware_type = _guess_firmware_type(name)
        self._flash_branch = (
            "beta" if self._get_existing_beta_channel() else "main"
        )
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
        """Confirm the user wants to flash the legacy device.

        We already know the IP (from zeroconf) and the firmware type (from
        the hostname). The form is just an informational confirmation —
        no fields to edit. If we can't guess the firmware type, abort
        rather than risk bricking the device with the wrong binary.
        """
        if not self._flash_firmware_type:
            return self.async_abort(
                reason="unknown_firmware_type",
                description_placeholders={"name": self._device_name or "device"},
            )

        if user_input is None:
            return self.async_show_form(
                step_id="flash_legacy",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "name": self._device_name or "",
                    "firmware_type": self._flash_firmware_type,
                },
            )

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
        return self.async_show_progress_done(next_step_id="post_flash_wait")

    async def async_step_post_flash_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the freshly-flashed device to reboot into new firmware.

        New firmware exposes /provision on HTTP, so we poll the same IP
        until it responds — typically 20-40s after a successful OTA. On
        success we drop into the existing zeroconf MQTT form so the user
        finishes provisioning in one continuous flow. We can't fall back
        to BLE because legacy devices were already on WiFi when flashed.
        """
        if self._post_flash_task is None:
            self._post_flash_task = self.hass.async_create_task(
                self._wait_for_provision_endpoint(
                    self._flash_host, timeout=120
                )
            )

        if not self._post_flash_task.done():
            return self.async_show_progress(
                step_id="post_flash_wait",
                progress_action="waiting_for_reboot",
                progress_task=self._post_flash_task,
                description_placeholders={
                    "name": self._device_name or "device"
                },
            )

        came_back = bool(self._post_flash_task.result())
        if not came_back:
            return self.async_show_progress_done(
                next_step_id="post_flash_unreachable"
            )

        # Treat the post-flash device exactly like a fresh zeroconf
        # discovery — it's on WiFi, on the same IP, with the new
        # firmware's /provision endpoint live.
        self._device_host = self._flash_host
        self._device_on_wifi = True
        return self.async_show_progress_done(next_step_id="zeroconf_confirm")

    async def async_step_post_flash_unreachable(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="post_flash_unreachable",
            description_placeholders={
                "host": self._flash_host or "",
                "name": self._device_name or "device",
            },
        )

    async def _wait_for_provision_endpoint(
        self, host: str | None, timeout: int = 120
    ) -> bool:
        """Poll http://<host>/provision until it answers (or timeout).

        Reuses the POST-based probe so it matches `_device_supports_provisioning`.
        2s sleep between attempts; the first 15-20s typically yield
        connection refused (boot in progress), then a normal HTTP
        response once WiFi is up and the web server is listening.
        """
        if not host:
            return False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        session = async_get_clientsession(self.hass)
        while loop.time() < deadline:
            if await _probe_provision_endpoint(session, host):
                return True
            await asyncio.sleep(2)
        return False

    async def async_step_flash_failure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="flash_failed",
            description_placeholders={"error": self._flash_error or "unknown"},
        )

    async def _device_supports_provisioning(self, host: str) -> bool:
        """Probe the device's HTTP /provision endpoint.

        Uses POST because some firmware revisions only register the route
        for POST and close the connection on other methods (the original
        GET-based probe got false negatives against post-OTA firmware).

        Detection rule: ESPHome's default web_server returns HTTP 500 with
        an empty body for unhandled routes. Anything else — including 411
        (length required), 400, 200, even a connection error after the
        request was sent — proves the route is registered.
        """
        return await _probe_provision_endpoint(
            async_get_clientsession(self.hass), host
        )

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
            # Post-flash re-entry: replay the creds the user already
            # typed before we routed them through the legacy flash.
            if self._pending_mqtt_creds is not None:
                creds = self._pending_mqtt_creds
                self._pending_mqtt_creds = None
                return await self.async_step_zeroconf_confirm(creds)
            return await self._show_zeroconf_mqtt_form()

        broker = user_input.get("mqtt_broker", "").strip()
        username = user_input.get("mqtt_username", "").strip()
        password = user_input.get("mqtt_password", "")

        if not broker:
            return await self._show_zeroconf_mqtt_form(
                errors={"mqtt_broker": "broker_required"}
            )

        # Send MQTT credentials via HTTP POST to the device
        success, looks_legacy = await self._provision_via_http(
            self._device_host, broker, username, password
        )
        if success:
            return await self.async_step_await_mqtt()

        # Legacy firmware (no /provision handler) → offer an in-flow
        # update. We need a guessed firmware type to know which bin to
        # download; without one we can't safely flash, so fall through
        # to the generic error.
        if looks_legacy and self._flash_firmware_type:
            self._pending_mqtt_creds = {
                "mqtt_broker": broker,
                "mqtt_username": username,
                "mqtt_password": password,
            }
            self._flash_task = None
            self._flash_error = None
            return await self.async_step_flash_legacy()

        return await self._show_zeroconf_mqtt_form(
            errors={"base": "http_provision_failed"}
        )

    async def _provision_via_http(
        self, host: str, broker: str, username: str, password: str
    ) -> tuple[bool, bool]:
        """Send MQTT credentials to the device via HTTP POST.

        Returns (success, looks_legacy). looks_legacy is True when the response
        pattern matches a device whose firmware lacks the /provision handler:
        ESPHome's default web_server returns 500 with empty body for unhandled
        POST routes, while new firmware returns 200 (ok) or 400 ({"error":...}).
        """
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
                    return True, False
                body = await resp.text()
                _LOGGER.error("HTTP provision failed: %s %s", resp.status, body)
                looks_legacy = resp.status == 500 and not body.strip()
                return False, looks_legacy
        except Exception:
            _LOGGER.exception("Failed to send MQTT config to %s via HTTP", host)
            return False, False

    async def _mqtt_status_online(self, device_id: str | None) -> bool:
        """True only if {prefix}/{device_id}/status currently reads "online".

        The status topic is backed by the device's MQTT last-will, so it
        reflects live connection state. The *config* topic must not be used for
        this — it's retained, so it lingers on the broker after a device goes
        offline (factory reset, power loss) and would make us treat a dead
        device as still provisioned, skipping re-provisioning.
        """
        if not device_id or "mqtt" not in self.hass.config.components:
            return False

        from homeassistant.components.mqtt import async_subscribe

        statuses: dict[str, bool] = {}

        def _on_status(msg):
            parts = msg.topic.split("/")
            if len(parts) >= 2:
                try:
                    statuses[parts[1]] = (
                        json.loads(msg.payload).get("state") == "online"
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass

        unsub = await async_subscribe(
            self.hass, f"{DEFAULT_MQTT_PREFIX}/+/{STATUS_TOPIC_SUFFIX}", _on_status, qos=0
        )
        try:
            await asyncio.sleep(2.5)  # let the retained status message arrive
            return statuses.get(device_id, False)
        finally:
            unsub()

    async def _check_device_on_mqtt_by_name(self, device_name: str) -> bool:
        """True only if a device with this id is currently online via MQTT."""
        return await self._mqtt_status_online(device_name)

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

        # Same suppression the zeroconf path does: a device already adopted by
        # the hub shouldn't raise a fresh "Discovered" card. The unique_id check
        # above can never catch this — it keys on the BLE MAC, whereas adoption
        # keys on the MQTT prefix (async_step_user) or the mDNS hostname
        # (async_step_zeroconf), so it never matches and every adopted ESP32
        # re-prompted forever as it advertised. Match on the WiFi MAC suffix
        # derived from the BLE address, which is stable across all three paths.
        if _is_mac_suffix_adopted(self.hass, self._ble_mac_to_wifi_suffix()):
            _LOGGER.info(
                "Device %s (%s) already adopted, dismissing BLE rediscovery",
                discovery_info.name, discovery_info.address,
            )
            return self.async_abort(reason="already_configured")

        self._device_name = self._friendly_ble_name(discovery_info)

        # Show the discovery confirmation to the user
        self.context["title_placeholders"] = {"name": self._device_name}
        return await self.async_step_bluetooth_confirm()

    def _friendly_ble_name(self, discovery_info: BluetoothServiceInfoBleak) -> str:
        """Best-effort display name for a BLE-discovered device.

        HA reports the MAC address as the device "name" when it didn't capture
        the advertised local name — SmartVan devices carry their name in the
        BLE scan response (no room beside the 128-bit service UUID in the
        primary advert), which passive scanning on the host adapter misses. In
        that case fall back to a label built from the WiFi-MAC suffix so the
        discovery card reads e.g. "SmartVan.io device (331070)" rather than a
        raw MAC. When HA *does* have the real name (active scan / BT proxy), we
        use it — it includes the device type, e.g. "smartvanio-in-331070".
        """
        name = (discovery_info.name or "").strip()
        addr_norm = discovery_info.address.replace(":", "").replace("-", "").lower()
        if name and name.replace(":", "").replace("-", "").lower() != addr_norm:
            return name
        suffix = self._ble_mac_to_wifi_suffix()
        return f"SmartVan.io device ({suffix})" if suffix else "SmartVan.io device"

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
        """True only if the discovered BLE device is currently ONLINE via MQTT.

        Maps the BLE MAC to the device_id via the (retained) config message,
        then confirms liveness through the LWT-backed status topic — never the
        retained config alone, which persists after a device goes offline.
        """
        if "mqtt" not in self.hass.config.components or not self._discovery_info:
            return False

        from homeassistant.components.mqtt import async_subscribe

        ble_mac = self._discovery_info.address.upper().replace("-", ":")
        # ESP32 BLE MAC = WiFi MAC + 2 (last octet)
        mac_parts = ble_mac.split(":")
        wifi_last_octet = int(mac_parts[-1], 16) - 2
        if wifi_last_octet < 0:
            wifi_last_octet += 256
        expected_wifi_mac = ":".join(mac_parts[:-1] + [f"{wifi_last_octet:02X}"])

        device_id: dict[str, str | None] = {"id": None}

        def _on_config(msg):
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, AttributeError):
                return
            device_mac = (payload.get("mac") or "").upper().replace("-", ":")
            if device_mac == expected_wifi_mac:
                device_id["id"] = payload.get("device_id")

        unsub = await async_subscribe(
            self.hass, f"{DEFAULT_MQTT_PREFIX}/+/config", _on_config, qos=0
        )
        try:
            await asyncio.sleep(1.5)  # learn device_id from the (retained) config
        finally:
            unsub()

        # Liveness is decided by the status topic, not the retained config.
        return await self._mqtt_status_online(device_id["id"])

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
        # Prefer the SSID the user just entered (preserved across a failed
        # attempt) so a retry doesn't start from a blank form.
        default_ssid = self._pending_wifi_ssid or ""
        if not default_ssid:
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

        # Device didn't appear in time — go straight back to the entry form
        # with an actionable error (instead of a dead-end empty screen). The
        # WiFi SSID the user just typed is preserved via _pending_wifi_ssid so
        # they only need to re-enter the password.
        if self._device_on_wifi:
            return await self._show_mqtt_only_form(
                errors={"base": "device_not_responding"}
            )
        return await self._show_full_form(
            errors={"base": "device_not_responding"}
        )

    async def _wait_for_mqtt_confirmation(self, timeout: int = 60) -> bool:
        """Subscribe to MQTT and wait for the device's config message.

        Matches on three independent signals so it works regardless of how
        the device announced itself before reaching MQTT:
          1. Exact `device_id` match (zeroconf devices already on new firmware).
          2. Full WiFi MAC match (BLE flows — derived from BLE MAC).
          3. MAC-suffix match (legacy device that was just flashed: hostname
             changed from e.g. `resistive_sensor-4a0afc` to
             `smartvanio-res-4a0afc`, but the trailing MAC suffix is stable).
        """
        if "mqtt" not in self.hass.config.components:
            return False

        from homeassistant.components.mqtt import async_subscribe

        expected_device_id: str | None = self._device_name
        expected_wifi_mac: str | None = None
        expected_mac_suffix: str | None = None

        if self._device_name and "-" in self._device_name:
            tail = self._device_name.rsplit("-", 1)[-1].lower()
            # Hostnames carry the last 3 octets of the MAC (6 hex chars).
            if len(tail) == 6 and all(c in "0123456789abcdef" for c in tail):
                expected_mac_suffix = tail

        if self._discovery_info is not None:
            ble_mac = self._discovery_info.address.upper().replace("-", ":")
            mac_parts = ble_mac.split(":")
            wifi_last_octet = int(mac_parts[-1], 16) - 2
            if wifi_last_octet < 0:
                wifi_last_octet += 256
            expected_wifi_mac = ":".join(
                mac_parts[:-1] + [f"{wifi_last_octet:02X}"]
            )

        if not (expected_device_id or expected_wifi_mac or expected_mac_suffix):
            return False

        found = asyncio.Event()

        def _on_message(msg):
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if expected_device_id and payload.get("device_id") == expected_device_id:
                found.set()
                return
            raw_mac = (payload.get("mac") or "").lower()
            if expected_wifi_mac:
                normalized = raw_mac.replace("-", ":").upper()
                if normalized == expected_wifi_mac:
                    found.set()
                    return
            if expected_mac_suffix:
                hex_only = raw_mac.replace(":", "").replace("-", "")
                if hex_only.endswith(expected_mac_suffix):
                    found.set()

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
        """Write WiFi + MQTT credentials to the device over BLE GATT.

        BlueZ sometimes hands back a stale or incomplete cached GATT table on
        the first connect, so the provisioning characteristic looks missing and
        the write raises BleakCharacteristicNotFoundError. When that happens we
        clear the device's GATT cache and reconnect, which forces a fresh
        service discovery — this is what makes provisioning reliable after the
        device has rebooted (its GATT handles change between boots).

        Connections go through bleak_retry_connector.establish_connection()
        rather than a bare BleakClient: BlueZ frequently fails or times out on
        the first connect to an ESP32 peripheral, and HA logs an explicit
        warning when you skip it. A bare connect made provisioning fail with
        asyncio.TimeoutError on marginal links (~-60 dBm on a Pi's built-in
        adapter) before a single byte was written.
        """
        if self._discovery_info is None:
            return False

        from bleak import BleakClient
        from bleak.exc import BleakCharacteristicNotFoundError
        from bleak_retry_connector import establish_connection

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

        address = self._discovery_info.address
        name_for_logs = self._device_name or address
        for attempt in range(3):
            client: BleakClient | None = None
            try:
                # establish_connection() retries internally and cooperates with
                # HA's connection slot accounting; a bare BleakClient() does not.
                client = await establish_connection(
                    BleakClient,
                    self._discovery_info.device,
                    name_for_logs,
                )
                char = client.services.get_characteristic(
                    BLE_MQTT_CONFIG_CHAR_UUID
                )
                if char is None:
                    # Stale/incomplete cached GATT — drop it and reconnect
                    # so the next attempt does a fresh service discovery.
                    _LOGGER.warning(
                        "Provisioning characteristic %s not found on %s "
                        "(attempt %d/3) — clearing GATT cache and retrying",
                        BLE_MQTT_CONFIG_CHAR_UUID, address, attempt + 1,
                    )
                    try:
                        await client.clear_cache()
                    except Exception:  # noqa: BLE001 - backend may lack it
                        _LOGGER.debug(
                            "clear_cache() unavailable for %s", address
                        )
                    continue
                await client.write_gatt_char(char, payload, response=True)
                _LOGGER.info("Credentials sent to %s via BLE", address)
                return True
            except BleakCharacteristicNotFoundError:
                _LOGGER.warning(
                    "BLE characteristic missing on %s (attempt %d/3) — retrying",
                    address, attempt + 1,
                )
                continue
            except Exception:
                # Previously this returned False, so a single flaky connect
                # (TimeoutError from BlueZ) aborted provisioning outright even
                # though the retry loop existed. Connection failures are
                # transient — keep trying.
                _LOGGER.warning(
                    "BLE connect/write to %s failed (attempt %d/3) — retrying",
                    address, attempt + 1, exc_info=True,
                )
                continue
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001 - already gone
                        _LOGGER.debug("Disconnect from %s failed", address)

        _LOGGER.error(
            "Could not provision %s over BLE after 3 attempts "
            "(connection failures or stale GATT cache?)", address,
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


async def _probe_provision_endpoint(session, host: str) -> bool:
    """True if http://{host}/provision is registered (i.e. new firmware).

    Sends an empty POST. ESPHome's default web_server returns HTTP 500 with
    an empty body for unhandled routes; anything else proves the route is
    registered (e.g. 411 length required, 400 missing fields, 200 ok).
    """
    url = f"http://{host}/provision"
    try:
        async with session.post(
            url, data=b"", timeout=5, allow_redirects=False
        ) as resp:
            if resp.status != 500:
                return True
            body = await resp.text()
            return bool(body.strip())
    except Exception:  # noqa: BLE001
        return False


def _hostname_mac_suffix(name: str) -> str | None:
    """Extract the trailing 6-hex-char MAC suffix from a device hostname.

    e.g. "resistive_sensor-4a0afc" → "4a0afc"
         "smartvanio-res-4a0afc"   → "4a0afc"
         "smartvanio-led-77a5a0"   → "77a5a0"
    Returns None if no recognisable suffix is present.
    """
    if not name or "-" not in name:
        return None
    tail = name.rsplit("-", 1)[-1].lower()
    if len(tail) == 6 and all(c in "0123456789abcdef" for c in tail):
        return tail
    return None


def _is_device_adopted(hass: HomeAssistant, device_id: str) -> bool:
    """True if any active hub entry already tracks this device.

    Matches on (1) exact device_id present in `store["devices"]`, or (2) any
    stored device whose `mac` ends in the same MAC suffix as the queried
    hostname. (2) is what catches the post-flash rename: the legacy
    firmware broadcast `resistive_sensor-4a0afc`, the new firmware broadcasts
    `smartvanio-res-4a0afc`, but the trailing 6 hex chars are stable.
    """
    store_root = hass.data.get(DOMAIN) or {}
    for entry_store in store_root.values():
        if not isinstance(entry_store, dict):
            continue
        devices = entry_store.get("devices") or {}
        if device_id in devices:
            return True
    return _is_mac_suffix_adopted(hass, _hostname_mac_suffix(device_id))


def _is_mac_suffix_adopted(hass: HomeAssistant, suffix: str | None) -> bool:
    """True if any adopted device's MAC ends with this 6-hex-char suffix.

    Split out of _is_device_adopted so the Bluetooth flow can reuse it. BLE
    can't go through the hostname path: the advertised name is truncated
    ("smartvanio-re-07876c" for "smartvanio-relay-07876c") and the abbreviation
    is ambiguous — "re" is both "relay" and "res" — while passive scanning may
    report no name at all. The MAC suffix is unambiguous and always derivable
    from the BLE address, so match on that instead.
    """
    if not suffix:
        return False
    store_root = hass.data.get(DOMAIN) or {}
    for entry_store in store_root.values():
        if not isinstance(entry_store, dict):
            continue
        for cfg in (entry_store.get("devices") or {}).values():
            mac_hex = (cfg.get("mac") or "").lower().replace(":", "").replace("-", "")
            if mac_hex.endswith(suffix):
                return True
    return False


def _build_bin_url(firmware_type: str, branch: str, filename: str) -> str:
    return (
        f"https://raw.githubusercontent.com/Smartvan-io/"
        f"{firmware_type}.bin/refs/heads/{branch}/{filename}"
    )


async def _discover_legacy_candidates(
    hass: HomeAssistant,
) -> list[dict[str, str]]:
    """Browse mDNS for SmartVan.io devices that can be (re)flashed.

    Browses two service types:
      - `_smartvaniolib._tcp` — newer SmartVan firmware (custom mDNS).
      - `_esphomelib._tcp`    — legacy/pre-MQTT firmware still on the stock
        ESPHome mDNS (i.e. adopted by the ESPHome integration). These are the
        devices that most need migrating.

    For the generic ESPHome service we only keep hosts whose name looks like a
    SmartVan device (firmware type guessable from the hostname), so unrelated
    ESPHome nodes — HA Voice, etc. — don't show up in the list.

    Each candidate is {name, host, firmware_type}.
    """

    def _blocking_browse() -> list[dict[str, str]]:
        from zeroconf import ServiceBrowser, Zeroconf
        import socket
        import threading

        results: dict[str, dict[str, str]] = {}
        done = threading.Event()

        def _record(name: str, info, require_smartvan: bool) -> None:
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
            fw_type = _guess_firmware_type(short_name) or ""
            # On the generic ESPHome service, skip nodes that aren't ours.
            if require_smartvan and not fw_type:
                return
            results[host] = {
                "name": short_name,
                "host": host,
                "firmware_type": fw_type,
            }

        class Listener:
            def __init__(self, require_smartvan: bool) -> None:
                self.require_smartvan = require_smartvan

            def add_service(self, zc: "Zeroconf", stype: str, name: str) -> None:
                try:
                    info = zc.get_service_info(stype, name, timeout=2000)
                except Exception:
                    return
                if info:
                    _record(name, info, self.require_smartvan)

            def remove_service(self, zc, stype, name): pass
            def update_service(self, zc, stype, name): pass

        zc = Zeroconf()
        browsers = [
            ServiceBrowser(
                zc, "_smartvaniolib._tcp.local.", Listener(require_smartvan=False)
            ),
            ServiceBrowser(
                zc, "_esphomelib._tcp.local.", Listener(require_smartvan=True)
            ),
        ]
        try:
            done.wait(timeout=4.0)
        finally:
            for browser in browsers:
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
    """Download firmware from GitHub and push it to the device.

    Prefers HTTP `/update` (port 80) since legacy firmware ships with
    `web_server` enabled and the same port is already known to be open
    (we just spoke to `/provision`). Falls back to native ESPHome OTA on
    TCP 3232 for builds without web_server.
    """
    from .ota_native import run_ota as run_native_ota
    from .ota_http import run_http_ota, has_update_endpoint, HTTPOTAError

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

    if await has_update_endpoint(session, host):
        _LOGGER.info(
            "Legacy rescue flash: uploading %d bytes to %s via HTTP /update",
            len(binary), host,
        )
        try:
            await run_http_ota(session, host, binary)
            _LOGGER.info("Legacy rescue flash: HTTP upload succeeded for %s", host)
            return
        except HTTPOTAError as err:
            _LOGGER.warning(
                "Legacy rescue flash: HTTP upload failed for %s (%s) — falling back to native OTA",
                host, err,
            )

    _LOGGER.info(
        "Legacy rescue flash: pushing %d bytes to %s via native OTA (TCP 3232)",
        len(binary), host,
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
