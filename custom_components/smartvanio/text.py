"""SmartVan.io Text platform.

Creates Home Assistant text entities from SmartVan.io device discovery.
Backs free-form string values that live on the device, such as the
resistive sensor's JSON interpolation points.

The device is the source of truth: the firmware declares these as
``template`` text components with ``restore_value: true``, so the value is
persisted in flash and survives reboots. This entity mirrors that value and
writes changes back over MQTT.

Topics (ESPHome native layout):
  State:   {device_id}/text/{channel}/state    -> raw string
  Command: {device_id}/text/{channel}/command  <- raw string
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_TEXT

_LOGGER = logging.getLogger(__name__)

# Home Assistant refuses to store a state string longer than 255 chars.
# ESPHome's text component defaults to the same limit, so this ceiling
# reflects what the device can actually hold rather than imposing a new one.
MAX_TEXT_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io text entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: dict[str, SmartVanText] = {}

    def _create_texts_from_config(device_id: str, config: dict) -> list[SmartVanText]:
        texts = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_TEXT:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            entity = SmartVanText(hass, device_id, channel, entity_config, config)
            texts.append(entity)
            created_entities[unique_id] = entity
            _LOGGER.info("Created text entity: %s", unique_id)
        return texts

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_texts_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_texts_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanText(TextEntity):
    """A free-form string value stored on the device, exposed over MQTT."""

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = MAX_TEXT_LENGTH

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
        self._attr_name = entity_config.get("name", f"Text {channel}")
        self._attr_native_value = entity_config.get("initial_value")
        self._attr_available = True
        if entity_config.get("entity_category") == "config":
            self._attr_entity_category = EntityCategory.CONFIG

        self._state_topic = f"{device_id}/text/{channel}/state"
        self._command_topic = f"{device_id}/text/{channel}/command"
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
            # ESPHome publishes the raw string. Tolerate a JSON envelope in
            # case a future firmware wraps it, matching the select platform.
            try:
                payload = json.loads(raw)
                value = payload.get("value") if isinstance(payload, dict) else str(raw)
            except (json.JSONDecodeError, ValueError):
                value = str(raw)
            if value is None:
                return
            if len(value) > MAX_TEXT_LENGTH:
                # Storing this would make HA reject the state outright and
                # drop the entity, so keep the last good value and say why.
                _LOGGER.warning(
                    "Ignoring %s state of %d chars — exceeds the %d-char limit",
                    self._attr_unique_id, len(value), MAX_TEXT_LENGTH,
                )
                return
            self._attr_native_value = value
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

    async def async_set_value(self, value: str) -> None:
        await mqtt.async_publish(
            self.hass, self._command_topic, value,
            qos=MQTT_QOS, retain=False,
        )
        # The firmware echoes the new value back on the state topic, but set
        # it optimistically so the UI doesn't appear to ignore the write.
        self._attr_native_value = value
        self.async_write_ha_state()
