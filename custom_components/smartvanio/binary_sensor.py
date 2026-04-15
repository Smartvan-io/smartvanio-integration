"""SmartVan.io Binary Sensor platform.

Creates Home Assistant binary sensor entities from SmartVan.io device discovery.
Handles door sensors (device_class="door") and push buttons (no device_class).
These are read-only — HA cannot command them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_BINARY_SENSOR

_LOGGER = logging.getLogger(__name__)

# Map device_class strings from the config payload to HA BinarySensorDeviceClass
_DEVICE_CLASS_MAP: dict[str, BinarySensorDeviceClass] = {
    "door":   BinarySensorDeviceClass.DOOR,
    "motion": BinarySensorDeviceClass.MOTION,
    "window": BinarySensorDeviceClass.WINDOW,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io binary sensors from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_binary_sensors_from_config(
        device_id: str, config: dict
    ) -> list[SmartVanBinarySensor]:
        entities = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_BINARY_SENSOR:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            entities.append(
                SmartVanBinarySensor(hass, device_id, channel, entity_config, config)
            )
            created_entities.add(unique_id)
            _LOGGER.info("Created binary_sensor entity: %s", unique_id)
        return entities

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_binary_sensors_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_binary_sensors_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanBinarySensor(BinarySensorEntity):
    """Representation of a SmartVan.io binary sensor (door or button) via MQTT."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        channel: str,
        entity_config: dict[str, Any],
        device_config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._channel = channel
        self._device_config = device_config

        self._attr_unique_id = f"{device_id}_{channel}"
        self._attr_name = entity_config.get("name", f"Sensor {channel}")
        self._attr_is_on = False
        self._attr_available = True

        raw_class = entity_config.get("device_class", "None")
        self._attr_device_class = _DEVICE_CLASS_MAP.get(raw_class)

        self._state_topic = f"{device_id}/binary_sensor/{channel}/state"
        self._status_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/status"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_config.get("name", f"SmartVan.io {self._device_id}"),
            manufacturer=MANUFACTURER,
            model=self._device_config.get("model", "Unknown"),
            sw_version=self._device_config.get("firmware", "Unknown"),
        )

    async def async_added_to_hass(self) -> None:
        @callback
        def _state_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    self._attr_is_on = payload.get("state", "OFF").upper() == "ON"
                else:
                    self._attr_is_on = str(raw).upper() == "ON"
            except (json.JSONDecodeError, ValueError):
                self._attr_is_on = str(raw).upper() == "ON"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic, _state_received, qos=MQTT_QOS)

        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._status_topic, _status_received, qos=MQTT_QOS)
