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

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
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
)

_LOGGER = logging.getLogger(__name__)

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
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            flow_unique = (flow.get("context") or {}).get("unique_id")
            flow_step = flow.get("step_id")
            if flow_unique == device_id and flow_step == "zeroconf_confirm":
                _LOGGER.debug(
                    "Dismissing idle discovery flow %s for already-adopted %s",
                    flow["flow_id"], device_id,
                )
                hass.async_create_task(
                    hass.config_entries.flow.async_abort(flow["flow_id"])
                )

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

    # Forward setup to platforms (light, switch, sensor, etc.)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("SmartVan.io integration setup complete")
    return True


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
