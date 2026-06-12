"""SmartVan.io Number platform.

Creates Home Assistant number entities from SmartVan.io device discovery.
Handles writable numeric values such as calibration offsets.

Topics:
  State:   smartvanio/{device_id}/number/{channel}/state  -> {"value": 0.0}
  Command: smartvanio/{device_id}/number/{channel}/set    <- {"value": 5.5}
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_NUMBER

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io number entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_numbers_from_config(device_id: str, config: dict) -> list[SmartVanNumber]:
        numbers = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_NUMBER:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            numbers.append(SmartVanNumber(hass, device_id, channel, entity_config, config))
            created_entities.add(unique_id)
            _LOGGER.info("Created number entity: %s", unique_id)
        return numbers

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_numbers_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_numbers_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanNumber(NumberEntity):
    """A writable numeric value exposed over MQTT."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

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
        self._attr_name = entity_config.get("name", f"Number {channel}")
        # Only constrain the range when the device explicitly supplies bounds.
        # Never fall back to a hardcoded range here: a generic default (e.g. the
        # inclinometer's -90..90) silently clamps unrelated entities such as the
        # resistive sensor's 0..15000 Ω numbers. If unset, HA's NumberEntity
        # default (0..100) applies, but well-behaved firmware always sends min/max.
        min_val = entity_config.get("min")
        max_val = entity_config.get("max")
        if min_val is not None:
            self._attr_native_min_value = min_val
        if max_val is not None:
            self._attr_native_max_value = max_val
        self._attr_native_step = entity_config.get("step", 0.1)
        self._attr_native_unit_of_measurement = entity_config.get("unit")
        self._attr_native_value = entity_config.get("min", 0)
        self._attr_available = True

        self._state_topic = f"{device_id}/number/{channel}/state"
        self._command_topic = f"{device_id}/number/{channel}/command"
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
                if isinstance(payload, dict) and "value" in payload:
                    self._attr_native_value = float(payload["value"])
                else:
                    self._attr_native_value = float(raw)
            except (json.JSONDecodeError, ValueError):
                try:
                    self._attr_native_value = float(raw)
                except (ValueError, TypeError):
                    return
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

    async def async_set_native_value(self, value: float) -> None:
        await mqtt.async_publish(
            self.hass, self._command_topic, str(value),
            qos=MQTT_QOS, retain=False,
        )
