"""SmartVan.io Light platform."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    MQTT_TOPIC_PREFIX,
    MQTT_QOS,
    ENTITY_TYPE_LIGHT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io lights from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_lights_from_config(device_id: str, config: dict) -> list[SmartVanLight]:
        lights = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_LIGHT:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            lights.append(SmartVanLight(hass, device_id, channel, entity_config, config))
            created_entities.add(unique_id)
            _LOGGER.info("Created light entity: %s", unique_id)
        return lights

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_lights_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_lights_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)

    # ── Segment entity creation ────────────────────────────────────
    # Subscribes to retained segment definitions published by the card.
    # Topic: smartvanio/{device_id}/light/{channel}/segments
    # Payload: [{"id": "...", "name": "...", "start": 0, "end": 30}, ...]

    @callback
    def _on_segments_received(msg: mqtt.ReceiveMessage) -> None:
        parts = msg.topic.split("/")
        # smartvanio / device_id / light / channel / segments
        if len(parts) != 5 or parts[4] != "segments":
            return
        device_id = parts[1]
        channel = parts[3]

        try:
            raw = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            _LOGGER.warning("Invalid segments JSON on %s", msg.topic)
            return

        # Support both old array format and new {max_leds, segments} wrapper
        if isinstance(raw, dict):
            segments = raw.get("segments", [])
        elif isinstance(raw, list):
            segments = raw
        else:
            return

        if not isinstance(segments, list):
            return

        device_config = store.get("pending_configs", {}).get(device_id, {})
        parent_unique_id = f"{device_id}_{channel}"
        entity_reg = er.async_get(hass)

        # parent_entity_id is embedded in each segment by the card when it saves.
        # Fall back to entity registry only if not present (old retained messages).
        parent_entity_id = next(
            (seg.get("parent_entity_id", "") for seg in segments if seg.get("parent_entity_id")),
            "",
        )
        if not parent_entity_id:
            parent_entity_id = entity_reg.async_get_entity_id("light", DOMAIN, parent_unique_id) or ""
        _LOGGER.debug("Segments for %s: parent_entity_id=%s", parent_unique_id, parent_entity_id)

        # Build the set of unique_ids that should exist after this update.
        uid_prefix = f"{device_id}_{channel}_seg_"
        incoming_unique_ids = {
            f"{uid_prefix}{seg['id']}"
            for seg in segments
            if seg.get("id")
        }

        # Remove stale segment entities that are no longer in the payload.
        for entry in list(entity_reg.entities.values()):
            if (
                entry.platform == DOMAIN
                and entry.domain == "light"
                and entry.unique_id.startswith(uid_prefix)
                and entry.unique_id not in incoming_unique_ids
            ):
                _LOGGER.info("Removing stale segment entity: %s", entry.unique_id)
                entity_reg.async_remove(entry.entity_id)
                created_entities.discard(entry.unique_id)

        # Create entities not yet instantiated this session.
        # created_entities is per-session (reset on HA restart), so entities are
        # always recreated after a restart even if they exist in the registry.
        new_entities = []
        for seg in segments:
            seg_id = seg.get("id", "")
            if not seg_id:
                continue
            unique_id = f"{uid_prefix}{seg_id}"
            if unique_id in created_entities:
                continue
            new_entities.append(
                SmartVanSegmentLight(
                    hass=hass,
                    device_id=device_id,
                    channel=channel,
                    segment_id=seg_id,
                    name=seg.get("name", f"Segment {seg_id}"),
                    start=int(seg.get("start", 0)),
                    end=int(seg.get("end", 0)),
                    parent_entity_id=parent_entity_id,
                    device_config=device_config,
                )
            )
            created_entities.add(unique_id)
            _LOGGER.info("Created segment light entity: %s (parent=%s)", unique_id, parent_entity_id)

        if new_entities:
            async_add_entities(new_entities)

    await mqtt.async_subscribe(
        hass,
        f"{MQTT_TOPIC_PREFIX}/+/light/+/segments",
        _on_segments_received,
        qos=MQTT_QOS,
    )


class SmartVanLight(LightEntity):
    """A SmartVan.io light controlled via MQTT."""

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
        self._entity_config = entity_config
        self._device_config = device_config

        self._attr_unique_id = f"{device_id}_{channel}"
        self._attr_name = entity_config.get("name", f"Light {channel}")

        supports_rgb = entity_config.get("supports_rgb", False)
        supports_brightness = entity_config.get("supports_brightness", True)

        if supports_rgb:
            self._attr_color_mode = ColorMode.RGB
            self._attr_supported_color_modes = {ColorMode.RGB}
        elif supports_brightness:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

        self._attr_is_on = False
        self._attr_brightness = 0
        self._attr_rgb_color = (255, 255, 255)
        self._attr_available = True
        self._max_leds: int = int(entity_config.get("max_leds", 0))
        self._last_seen: float = time.monotonic()

        # ESPHome publishes light state/commands on native topics (device_id as prefix)
        self._state_topic = f"{device_id}/light/{channel}/state"
        self._command_topic = f"{device_id}/light/{channel}/command"
        # Discovery and status still use the smartvanio/ prefix
        self._status_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/status"
        self._segments_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/light/{channel}/segments"

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
        attrs: dict[str, Any] = {"last_brightness": self._attr_brightness}
        if self._max_leds:
            attrs["max_leds"] = self._max_leds
        return attrs

    async def async_added_to_hass(self) -> None:
        @callback
        def _state_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._update_from_payload(payload)
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

        @callback
        def _check_heartbeat(_now) -> None:
            elapsed = time.monotonic() - self._last_seen
            if elapsed > 90 and self._attr_available:
                self._attr_available = False
                self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_interval(self.hass, _check_heartbeat, timedelta(seconds=30))
        )

        @callback
        def _segments_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                raw = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(raw, dict):
                max_leds = int(raw.get("max_leds", 0))
                if max_leds and max_leds != self._max_leds:
                    self._max_leds = max_leds
                    self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._segments_topic, _segments_received, qos=MQTT_QOS)

        # Reflect aggregate segment state on the parent switch
        @callback
        def _on_segment_state_changed(event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if new_state.attributes.get("smartvanio_parent_entity_id") != self.entity_id:
                return
            any_on = any(
                s.state == "on"
                for s in self.hass.states.async_all("light")
                if s.attributes.get("smartvanio_parent_entity_id") == self.entity_id
            )
            self._attr_is_on = any_on
            self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen("state_changed", _on_segment_state_changed)
        )

    def _update_from_payload(self, payload: dict[str, Any]) -> None:
        if "state" in payload:
            self._attr_is_on = payload["state"].upper() == "ON"
        if "brightness" in payload:
            brightness = max(0, min(255, int(payload["brightness"])))
            # Only update if non-zero: devices commonly report brightness=0 in the
            # OFF state, which would reset last_brightness and move the slider to 0.
            if brightness > 0:
                self._attr_brightness = brightness
        if "color" in payload and self._attr_is_on:
            # Only update colour when the light is on. Devices typically report
            # their default colour (e.g. white) in the OFF state payload, which
            # would overwrite the last meaningful colour and lose it visually.
            color = payload["color"]
            self._attr_rgb_color = (
                max(0, min(255, int(color.get("r", 255)))),
                max(0, min(255, int(color.get("g", 255)))),
                max(0, min(255, int(color.get("b", 255)))),
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Turn on all child segment entities first
        my_entity_id = self.entity_id
        segment_ids = [
            s.entity_id
            for s in self.hass.states.async_all("light")
            if s.attributes.get("smartvanio_parent_entity_id") == my_entity_id
        ]
        if segment_ids:
            await self.hass.services.async_call(
                "light", "turn_on", {"entity_id": segment_ids}, blocking=True
            )
        payload: dict[str, Any] = {"state": "ON"}
        if ATTR_BRIGHTNESS in kwargs:
            payload["brightness"] = kwargs[ATTR_BRIGHTNESS]
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            payload["color"] = {"r": r, "g": g, "b": b}
            self._attr_rgb_color = (r, g, b)
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._publish_command(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Turn off all child segment entities first
        my_entity_id = self.entity_id
        segment_ids = [
            s.entity_id
            for s in self.hass.states.async_all("light")
            if s.attributes.get("smartvanio_parent_entity_id") == my_entity_id
        ]
        if segment_ids:
            await self.hass.services.async_call(
                "light", "turn_off", {"entity_id": segment_ids}, blocking=True
            )
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._publish_command({"state": "OFF"})

    async def _publish_command(self, payload: dict[str, Any]) -> None:
        _LOGGER.debug("Publishing to %s: %s", self._command_topic, json.dumps(payload))
        await mqtt.async_publish(self.hass, self._command_topic, json.dumps(payload), qos=MQTT_QOS, retain=False)


class SmartVanSegmentLight(LightEntity, RestoreEntity):
    """A virtual light entity representing a segment of a parent LED strip.

    Publishes commands to the parent channel's set topic with a 'segment'
    field so the firmware can address only the LEDs in the specified range.
    """

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        channel: str,
        segment_id: str,
        name: str,
        start: int,
        end: int,
        parent_entity_id: str,
        device_config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._channel = channel
        self._segment_id = segment_id
        self._start = start
        self._end = end
        self._parent_entity_id: str = parent_entity_id
        self._device_config = device_config

        self._attr_unique_id = f"{device_id}_{channel}_seg_{segment_id}"
        self._attr_name = name
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_rgb_color = (255, 255, 255)
        self._attr_available = True
        self._last_seen: float = time.monotonic()

        self._command_topic = f"{device_id}/light/{channel}/command"
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "smartvanio_parent_entity_id": self._parent_entity_id,
            "segment_start": self._start,
            "segment_end": self._end,
            "segment_id": self._segment_id,
        }

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state:
            self._attr_is_on = last_state.state == "on"
            if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = int(brightness)
            if (rgb := last_state.attributes.get(ATTR_RGB_COLOR)) is not None:
                self._attr_rgb_color = (int(rgb[0]), int(rgb[1]), int(rgb[2]))

        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            is_online = payload.get("state") == "online"
            if is_online:
                self._last_seen = time.monotonic()
            self._attr_available = is_online
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._status_topic, _status_received, qos=MQTT_QOS)

        @callback
        def _check_heartbeat(_now) -> None:
            elapsed = time.monotonic() - self._last_seen
            if elapsed > 90 and self._attr_available:
                self._attr_available = False
                self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_interval(self.hass, _check_heartbeat, timedelta(seconds=30))
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_RGB_COLOR in kwargs:
            self._attr_rgb_color = kwargs[ATTR_RGB_COLOR]
        self._attr_is_on = True
        r, g, b = self._attr_rgb_color
        payload = {
            "state": "ON",
            "brightness": self._attr_brightness,
            "color": {"r": r, "g": g, "b": b},
            "segment": {"start": self._start, "end": self._end},
        }
        await mqtt.async_publish(self.hass, self._command_topic, json.dumps(payload), qos=MQTT_QOS, retain=False)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        payload = {"state": "OFF", "segment": {"start": self._start, "end": self._end}}
        await mqtt.async_publish(self.hass, self._command_topic, json.dumps(payload), qos=MQTT_QOS, retain=False)
        self.async_write_ha_state()
