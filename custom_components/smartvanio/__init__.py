"""SmartVan.io — Campervan Smart Control System integration for Home Assistant.

This integration discovers SmartVan.io devices via MQTT and creates HA entities
for lights, switches, and sensors. It bridges MQTT JSON messages to native
HA entity states.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

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

    # Store runtime data for this integration
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "devices": {},
        "pending_configs": {},
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

        # Fire event so platforms can pick up new entities
        hass.bus.async_fire(
            f"{DOMAIN}_device_discovered",
            {"device_id": device_id, "config": payload},
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

    @callback
    def _handle_status(msg: mqtt.ReceiveMessage) -> None:
        """Handle device status (online/offline) messages."""
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            return

        # Extract device_id from topic: smartvanio/{device_id}/status
        parts = msg.topic.split("/")
        if len(parts) >= 2:
            device_id = parts[1]
            state = payload.get("state", "offline")
            _LOGGER.debug("Device %s status: %s", device_id, state)

            # Fire availability event
            hass.bus.async_fire(
                f"{DOMAIN}_device_status",
                {"device_id": device_id, "available": state == "online"},
            )

    try:
        await mqtt.async_subscribe(
            hass, status_topic, _handle_status, qos=MQTT_QOS
        )
    except Exception as err:
        raise ConfigEntryNotReady(
            "MQTT is not ready — will retry automatically"
        ) from err

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
