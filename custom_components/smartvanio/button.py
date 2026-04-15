"""SmartVan.io Button platform.

Creates Home Assistant button entities from SmartVan.io device discovery.
Handles momentary actions such as calibrate/reset triggers.

Topics:
  Command: smartvanio/{device_id}/button/{channel}/press  <- {}
  (buttons have no state topic)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_BUTTON

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io button entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_buttons_from_config(device_id: str, config: dict) -> list[SmartVanButton]:
        buttons = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_BUTTON:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            buttons.append(SmartVanButton(hass, device_id, channel, entity_config, config))
            created_entities.add(unique_id)
            _LOGGER.info("Created button entity: %s", unique_id)
        return buttons

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_buttons_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_buttons_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanButton(ButtonEntity):
    """A momentary action exposed over MQTT."""

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
        self._attr_name = entity_config.get("name", f"Button {channel}")
        self._attr_icon = entity_config.get("icon")
        self._attr_available = True

        self._command_topic = f"{device_id}/button/{channel}/command"
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
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._status_topic, _status_received, qos=MQTT_QOS)

    async def async_press(self) -> None:
        await mqtt.async_publish(
            self.hass, self._command_topic, "PRESS",
            qos=MQTT_QOS, retain=False,
        )
