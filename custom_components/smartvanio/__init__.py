"""SmartVan.io — Campervan Smart Control System integration for Home Assistant.

This integration discovers SmartVan.io devices via MQTT and creates HA entities
for lights, switches, and sensors. It bridges MQTT JSON messages to native
HA entity states.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import bluetooth, mqtt
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    MANUFACTURER,
    MQTT_TOPIC_PREFIX,
    MQTT_QOS,
    PLATFORMS,
    DISCOVERY_TOPIC_SUFFIX,
    STATUS_TOPIC_SUFFIX,
    ENTITY_TYPE_LIGHT,
    BLE_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


def reprovision_issue_id(device_id: str) -> str:
    """Repair-issue id for a device that needs re-provisioning."""
    return f"reprovision_{device_id}"


def _wifi_suffix_from_ble_mac(ble_mac: str) -> str | None:
    """Map a device's BLE MAC to its WiFi MAC suffix.

    ESP32 BLE MAC = WiFi MAC + 2 on the last octet. Device ids end in the
    last 3 octets of the WiFi MAC (e.g. ...-77a5a0), so this lets us match a
    BLE advertisement back to an already-registered device_id.
    """
    try:
        parts = ble_mac.upper().replace("-", ":").split(":")
        last = int(parts[-1], 16) - 2
        if last < 0:
            last += 256
        return (parts[-3] + parts[-2] + f"{last:02X}").lower()
    except (ValueError, IndexError):
        return None

# Store discovered devices and their config payloads
# Keyed by device_id
type SmartVanConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: SmartVanConfigEntry) -> bool:
    """Set up SmartVan.io from a config entry."""
    _LOGGER.info("Setting up SmartVan.io integration")
    _LOGGER.warning(
        "SmartVan.io integration is no longer distributed via HACS. "
        "Please install the SmartVan.io add-on "
        "(https://github.com/Smartvan-io/smartvan) which manages this "
        "integration, MQTT, and the dashboard cards automatically."
    )

    # Store runtime data for this integration
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "devices": {},
        "pending_configs": {},
        "device_availability": {},  # device_id -> {"available": bool, "last_seen": float}
    }

    # Subscribe to discovery topic for all smartvanio devices
    # Topic pattern: smartvanio/+/config
    discovery_topic = f"{MQTT_TOPIC_PREFIX}/+/{DISCOVERY_TOPIC_SUFFIX}"
    _LOGGER.debug("Subscribing to discovery topic: %s", discovery_topic)

    @callback
    def _handle_discovery(msg: mqtt.ReceiveMessage) -> None:
        """Handle incoming device discovery messages."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid JSON in discovery message: %s", msg.topic)
            return

        device_id = payload.get("device_id")
        if not device_id:
            _LOGGER.warning("Discovery payload missing device_id: %s", payload)
            return

        _LOGGER.info(
            "Discovered SmartVan.io device: %s (%s)",
            payload.get("name", device_id),
            device_id,
        )

        # Register the device in HA device registry
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_id)},
            name=payload.get("name", f"SmartVan.io {device_id}"),
            manufacturer=MANUFACTURER,
            model=payload.get("model", "Unknown"),
            sw_version=payload.get("firmware", "Unknown"),
        )

        # Store config and notify platforms
        store = hass.data[DOMAIN][entry.entry_id]
        store["devices"][device_id] = payload
        store["pending_configs"][device_id] = payload

        # Publishing config means the device is online and provisioned, so
        # clear any "needs re-provisioning" repair we may have raised for it.
        ir.async_delete_issue(hass, DOMAIN, reprovision_issue_id(device_id))

        # Refresh heartbeat — config messages prove the device is alive
        avail = store["device_availability"]
        avail.setdefault(device_id, {"available": False, "last_seen": 0})
        avail[device_id]["last_seen"] = time.monotonic()
        if not avail[device_id]["available"]:
            avail[device_id]["available"] = True
            _LOGGER.debug("Device %s availability: True (via config)", device_id)
            hass.bus.async_fire(
                f"{DOMAIN}_device_status",
                {"device_id": device_id, "available": True},
            )

        # Fire event so platforms can pick up new entities
        hass.bus.async_fire(
            f"{DOMAIN}_device_discovered",
            {"device_id": device_id, "config": payload},
        )

        # Dismiss any zeroconf "Discovered" card whose unique_id matches the
        # device_id we just registered. This catches the post-flash rename
        # case: legacy firmware broadcast `resistive_sensor-XXXXXX`, the
        # adoption flow ran under that unique_id, then the new firmware
        # broadcasts `smartvanio-res-XXXXXX` and HA spawned a fresh idle
        # discovery card. We only abort flows still parked at the initial
        # zeroconf_confirm step — never one the user is mid-flight on.
        # Collect first, then abort — async_abort() is a synchronous callback
        # that mutates the in-progress flow set, so aborting while iterating
        # async_progress_by_handler() would change the set mid-iteration.
        stale_flow_ids = [
            flow["flow_id"]
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if (flow.get("context") or {}).get("unique_id") == device_id
            and flow.get("step_id") == "zeroconf_confirm"
        ]
        for flow_id in stale_flow_ids:
            _LOGGER.debug(
                "Dismissing idle discovery flow %s for already-adopted %s",
                flow_id, device_id,
            )
            hass.config_entries.flow.async_abort(flow_id)

    try:
        await mqtt.async_subscribe(
            hass, discovery_topic, _handle_discovery, qos=MQTT_QOS
        )
    except Exception as err:
        raise ConfigEntryNotReady(
            "MQTT is not ready — will retry automatically"
        ) from err

    # Subscribe to status topic for availability tracking
    status_topic = f"{MQTT_TOPIC_PREFIX}/+/{STATUS_TOPIC_SUFFIX}"
    store = hass.data[DOMAIN][entry.entry_id]
    availability = store["device_availability"]

    @callback
    def _handle_status(msg: mqtt.ReceiveMessage) -> None:
        """Handle device status (online/offline) messages."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            return

        parts = msg.topic.split("/")
        if len(parts) >= 2:
            device_id = parts[1]
            is_online = payload.get("state") == "online"
            now = time.monotonic()
            prev = availability.get(device_id, {}).get("available")
            availability[device_id] = {"available": is_online, "last_seen": now}

            if is_online:
                ir.async_delete_issue(
                    hass, DOMAIN, reprovision_issue_id(device_id)
                )

            if prev != is_online:
                _LOGGER.debug("Device %s availability: %s", device_id, is_online)
                hass.bus.async_fire(
                    f"{DOMAIN}_device_status",
                    {"device_id": device_id, "available": is_online},
                )

    try:
        await mqtt.async_subscribe(
            hass, status_topic, _handle_status, qos=MQTT_QOS
        )
    except Exception as err:
        raise ConfigEntryNotReady(
            "MQTT is not ready — will retry automatically"
        ) from err

    # Heartbeat checker — mark devices unavailable if no status in 90s
    @callback
    def _check_heartbeats(_now) -> None:
        now = time.monotonic()
        for device_id, info in availability.items():
            if info["available"] and (now - info["last_seen"]) > 90:
                info["available"] = False
                _LOGGER.debug("Device %s heartbeat timeout — marking unavailable", device_id)
                hass.bus.async_fire(
                    f"{DOMAIN}_device_status",
                    {"device_id": device_id, "available": False},
                )

    entry.async_on_unload(
        async_track_time_interval(hass, _check_heartbeats, timedelta(seconds=30))
    )

    # Watch for known-but-offline devices that have dropped back into BLE
    # setup mode (factory reset, WiFi/router change) and raise a Repair so the
    # user can re-provision them without deleting the device.
    _setup_reprovision_watch(hass, entry)

    # Forward setup to platforms (light, switch, sensor, etc.)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("SmartVan.io integration setup complete")
    return True


@callback
def _setup_reprovision_watch(hass: HomeAssistant, entry: SmartVanConfigEntry) -> None:
    """Raise a Repair when a known, offline device re-enters BLE setup mode.

    A SmartVan.io device always advertises its provisioning service over BLE.
    If we see a device whose BLE MAC maps to an already-registered device that
    is currently MQTT-offline, it has lost its connection (factory reset, WiFi
    change, etc.) and is waiting to be set up again — surface that to the user.
    """
    store = hass.data[DOMAIN][entry.entry_id]
    store["ble_adverts"] = {}

    @callback
    def _on_advert(service_info, change) -> None:
        suffix = _wifi_suffix_from_ble_mac(service_info.address)
        if not suffix:
            return
        device_id = next(
            (d for d in store["devices"] if d.lower().endswith(suffix)), None
        )
        if device_id is None:
            return  # not a device we know about — leave to normal discovery
        if store["device_availability"].get(device_id, {}).get("available"):
            return  # online — nothing wrong
        # Known + offline + advertising setup service → needs re-provisioning.
        store["ble_adverts"][device_id] = service_info.address
        name = store["devices"].get(device_id, {}).get("name", device_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            reprovision_issue_id(device_id),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_offline_reprovision",
            translation_placeholders={"name": name},
            data={
                "device_id": device_id,
                "entry_id": entry.entry_id,
                "ble_address": service_info.address,
                "name": name,
            },
        )

    try:
        entry.async_on_unload(
            bluetooth.async_register_callback(
                hass,
                _on_advert,
                BluetoothCallbackMatcher(
                    service_uuid=BLE_SERVICE_UUID, connectable=True
                ),
                BluetoothScanningMode.ACTIVE,
            )
        )
    except Exception:  # noqa: BLE001 - bluetooth may be unavailable on this host
        _LOGGER.debug("Bluetooth unavailable — re-provision watch not registered")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Allow the user to delete a SmartVan.io device from the UI.

    Returning True surfaces the trash-can button on the device card. Stale
    devices (e.g. one reflashed to a different name, or removed from the
    network) can then be cleaned up without having to remove the whole
    integration. We also drop the device from our runtime store so a
    re-discovery on the same device_id starts from a clean slate.
    """
    device_ids = {ident[1] for ident in device_entry.identifiers if ident[0] == DOMAIN}
    store = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if store:
        for device_id in device_ids:
            store.get("devices", {}).pop(device_id, None)
            store.get("pending_configs", {}).pop(device_id, None)
            store.get("device_availability", {}).pop(device_id, None)
            store.get("ble_adverts", {}).pop(device_id, None)

    for device_id in device_ids:
        ir.async_delete_issue(hass, DOMAIN, reprovision_issue_id(device_id))

    # Clear retained MQTT state so the device doesn't immediately
    # reappear on next HA restart from the broker's retained config.
    if "mqtt" in hass.config.components:
        for device_id in device_ids:
            for suffix in (DISCOVERY_TOPIC_SUFFIX, STATUS_TOPIC_SUFFIX):
                topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/{suffix}"
                try:
                    await mqtt.async_publish(hass, topic, "", qos=MQTT_QOS, retain=True)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Failed to clear retained topic %s", topic)

    return True
