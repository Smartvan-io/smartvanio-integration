"""SmartVan.io Scene platform.

Scenes are stored as a retained JSON array on the MQTT topic
  smartvanio/{device_id}/scenes

Each entry:
  {
    "id": "unique_string",
    "name": "Scene name",
    "icon": "mdi:candle",         (optional)
    "lights": [
      {
        "entity_id": "light.xxx",
        "state": "ON",
        "brightness": 128,
        "rgb_color": [255, 120, 60]   (optional)
      },
      ...
    ]
  }

When async_activate() is called HA sets the entity state to the current
timestamp — this is the standard HA scene behaviour and is used by the
dashboard tile to display "last activated".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    MQTT_TOPIC_PREFIX,
    MQTT_QOS,
    SCENES_TOPIC_SUFFIX,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io scenes from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    # Maps unique_id -> SmartVanScene so we can push updates to existing entities
    entity_map: dict[str, "SmartVanScene"] = {}

    @callback
    def _on_scenes_received(msg: mqtt.ReceiveMessage) -> None:
        parts = msg.topic.split("/")
        # smartvanio / device_id / scenes
        if len(parts) != 3 or parts[2] != SCENES_TOPIC_SUFFIX:
            return
        device_id = parts[1]

        try:
            scenes_list = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid scenes JSON on %s", msg.topic)
            return

        if not isinstance(scenes_list, list):
            return

        device_config = store.get("pending_configs", {}).get(device_id, {})
        new_entities = []

        for scene_def in scenes_list:
            scene_id = scene_def.get("id", "")
            if not scene_id:
                continue
            unique_id = f"{device_id}_scene_{scene_id}"
            if unique_id in entity_map:
                # Entity already exists — update its definition in-place
                entity_map[unique_id].update_scene_def(scene_def)
            else:
                entity = SmartVanScene(
                    hass=hass,
                    device_id=device_id,
                    scene_id=scene_id,
                    scene_def=scene_def,
                    device_config=device_config,
                )
                entity_map[unique_id] = entity
                new_entities.append(entity)
                _LOGGER.info("Created scene entity: %s", unique_id)

        if new_entities:
            async_add_entities(new_entities)

    scenes_topic = f"{MQTT_TOPIC_PREFIX}/+/{SCENES_TOPIC_SUFFIX}"
    await mqtt.async_subscribe(hass, scenes_topic, _on_scenes_received, qos=MQTT_QOS)


class SmartVanScene(Scene):
    """A SmartVan.io scene that activates a set of light states."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        scene_id: str,
        scene_def: dict[str, Any],
        device_config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._scene_id = scene_id
        self._scene_def = scene_def
        self._device_config = device_config

        self._attr_unique_id = f"{device_id}_scene_{scene_id}"
        self._attr_name = scene_def.get("name", f"Scene {scene_id}")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_config.get("name", f"SmartVan.io {self._device_id}"),
            manufacturer=MANUFACTURER,
            model=self._device_config.get("model", "Unknown"),
            sw_version=self._device_config.get("firmware", "Unknown"),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lights = self._scene_def.get("lights", [])
        return {
            "scene_id": self._scene_id,
            "icon": self._scene_def.get("icon", "mdi:palette"),
            "light_count": len(lights),
            "lights": lights,
        }

    @callback
    def update_scene_def(self, scene_def: dict[str, Any]) -> None:
        """Update the scene definition and push new state to HA."""
        self._scene_def = scene_def
        self._attr_name = scene_def.get("name", f"Scene {self._scene_id}")
        if self.hass:
            self.async_write_ha_state()

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the scene by applying stored light states."""
        lights = self._scene_def.get("lights", [])
        for light in lights:
            entity_id = light.get("entity_id")
            if not entity_id:
                continue
            if light.get("state", "ON").upper() == "OFF":
                await self.hass.services.async_call(
                    "light", "turn_off", {"entity_id": entity_id}, blocking=True
                )
            else:
                service_data: dict[str, Any] = {"entity_id": entity_id}
                if "brightness" in light:
                    service_data["brightness"] = light["brightness"]
                if "rgb_color" in light:
                    service_data["rgb_color"] = light["rgb_color"]
                await self.hass.services.async_call(
                    "light", "turn_on", service_data, blocking=True
                )
        _LOGGER.debug("Activated scene %s", self._attr_unique_id)
