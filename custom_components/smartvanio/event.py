"""SmartVan.io Event platform.

Exposes physical-button gestures (single click, double click, hold) as
Home Assistant event entities so they show up in the automation UI as
selectable triggers.

Topics:
  State: smartvanio/{device_id}/event/{channel}/state  -> {"event":"single_click"}
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_EVENT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io event entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_events_from_config(device_id: str, config: dict) -> list[SmartVanEvent]:
        events = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_EVENT:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            events.append(SmartVanEvent(hass, device_id, channel, entity_config, config))
            created_entities.add(unique_id)
            _LOGGER.info("Created event entity: %s", unique_id)
        return events

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_events_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_events_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanEvent(EventEntity):
    """A physical button exposed as an HA event entity.

    Each MQTT message on the state topic fires one event with the
    `event` field as the event type. The event_types list is declared
    in the discovery payload so HA's automation UI shows each gesture
    as a separate trigger option.
    """

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
        self._attr_name = entity_config.get("name", f"Event {channel}")
        self._attr_icon = entity_config.get("icon")
        self._attr_event_types = entity_config.get(
            "event_types", ["single_click", "double_click", "button_hold"]
        )
        self._attr_available = True

        self._state_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/event/{channel}/state"
        self._status_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/status"
        # Paired binary_sensor topic — clearing event state on physical release
        # means the entity reflects "off" once the user lets go, instead of
        # sticking at the last gesture forever.
        self._release_topic = f"{device_id}/binary_sensor/{channel}/state"

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
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.warning("Invalid event payload on %s: %s", msg.topic, msg.payload)
                return
            event_type = payload.get("event")
            if not event_type:
                return
            if event_type not in self._attr_event_types:
                _LOGGER.debug(
                    "Event %s on %s not in declared event_types %s — firing anyway",
                    event_type, self._attr_unique_id, self._attr_event_types,
                )
            self._trigger_event(event_type)
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic, _state_received, qos=MQTT_QOS)

        @callback
        def _release_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            try:
                payload = json.loads(raw)
                is_on = isinstance(payload, dict) and str(payload.get("state", "")).upper() == "ON"
            except (json.JSONDecodeError, ValueError):
                is_on = str(raw).strip().upper() == "ON"
            if not is_on:
                self._attr_event_type = None
                self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._release_topic, _release_received, qos=MQTT_QOS)

        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._status_topic, _status_received, qos=MQTT_QOS)
