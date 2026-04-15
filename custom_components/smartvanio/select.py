"""SmartVan.io Select platform.

Creates Home Assistant select entities from SmartVan.io device discovery.
Handles enumerated choices such as sensor orientation.

Topics:
  State:   smartvanio/{device_id}/select/{channel}/state  -> {"value": "Option 1"}
  Command: smartvanio/{device_id}/select/{channel}/set    <- {"value": "Option 2"}
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_SELECT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io select entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: dict[str, SmartVanSelect] = {}

    def _create_selects_from_config(device_id: str, config: dict) -> list[SmartVanSelect]:
        selects = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_SELECT:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                # Entity already exists — update options if the config now provides them
                existing = created_entities[unique_id]
                new_options = entity_config.get("options", [])
                if new_options and existing.options != new_options:
                    existing._attr_options = new_options
                    if existing._attr_current_option and existing._attr_current_option not in new_options:
                        existing._attr_current_option = new_options[0]
                    existing.async_write_ha_state()
                    _LOGGER.info("Updated options for select entity: %s", unique_id)
                continue
            entity = SmartVanSelect(hass, device_id, channel, entity_config, config)
            selects.append(entity)
            created_entities[unique_id] = entity
            _LOGGER.info("Created select entity: %s", unique_id)
        return selects

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_selects_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_selects_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanSelect(SelectEntity):
    """An enumerated choice exposed over MQTT."""

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
        self._attr_name = entity_config.get("name", f"Select {channel}")
        self._attr_options = entity_config.get("options", [])
        self._attr_current_option = self._attr_options[0] if self._attr_options else None
        self._attr_available = True

        self._state_topic = f"{device_id}/select/{channel}/state"
        self._command_topic = f"{device_id}/select/{channel}/command"
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
                    value = payload.get("value")
                else:
                    value = str(raw)
            except (json.JSONDecodeError, ValueError):
                value = str(raw)
            # Learn options dynamically from incoming state values
            if value and value not in self._attr_options:
                self._attr_options = [*self._attr_options, value]
            if value:
                self._attr_current_option = value
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

    async def async_select_option(self, option: str) -> None:
        await mqtt.async_publish(
            self.hass, self._command_topic, option,
            qos=MQTT_QOS, retain=False,
        )
