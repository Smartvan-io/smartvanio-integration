"""SmartVan.io Sensor platform.

Creates Home Assistant sensor entities from SmartVan.io device discovery.
Handles tank levels (water, gas, waste) and any other numeric sensors
declared with entity type "sensor" in the config payload.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
from scipy.interpolate import interp1d

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_SENSOR

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io sensors from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: set[str] = set()

    def _create_sensors_from_config(device_id: str, config: dict) -> list[SmartVanSensor]:
        sensors = []
        for entity_config in config.get("entities", []):
            if entity_config.get("type") != ENTITY_TYPE_SENSOR:
                continue
            channel = entity_config.get("channel", "unknown")
            unique_id = f"{device_id}_{channel}"
            if unique_id in created_entities:
                continue
            if entity_config.get("sensor_type") == "tank_level":
                sensors.append(SmartVanTankLevelSensor(hass, device_id, channel, entity_config, config))
            elif entity_config.get("sensor_type") == "tank_config":
                sensors.append(SmartVanTankConfigSensor(hass, device_id, channel, entity_config, config))
            elif entity_config.get("sensor_type") == "tank_kind":
                sensors.append(SmartVanTextSensor(hass, device_id, channel, entity_config, config))
            else:
                sensors.append(SmartVanSensor(hass, device_id, channel, entity_config, config))
            created_entities.add(unique_id)
            _LOGGER.info("Created sensor entity: %s", unique_id)
        return sensors

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_sensors_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_sensors_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanSensor(SensorEntity):
    """Representation of a SmartVan.io numeric sensor via MQTT."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

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
        self._attr_native_unit_of_measurement = entity_config.get("unit")
        self._attr_native_value = None
        self._attr_available = True

        self._state_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/sensor/{channel}/state"
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
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_native_value = payload.get("value")
            # Unit can be overridden by the device payload
            if "unit" in payload:
                self._attr_native_unit_of_measurement = payload["unit"]
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


class SmartVanTextSensor(SensorEntity):
    """A sensor that holds a string state (no measurement state class)."""

    _attr_has_entity_name = True
    _attr_state_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        self._attr_native_value = None
        self._attr_available = True

        self._state_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/sensor/{channel}/state"
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
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_native_value = payload.get("value")
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


class SmartVanTankLevelSensor(SensorEntity):
    """Tank level (%) computed in HA by interpolating raw voltage against calibration points.

    Subscribes to three MQTT topics for the same tank:
      - ``sensor/{channel}_voltage/state``  — raw ADC voltage
      - ``sensor/{channel}_config/state``   — JSON calibration points ``[[v, pct], ...]``
      - ``sensor/{channel}_kind/state``     — interpolation kind string

    Recomputes and updates state whenever any of the three change.
    """

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

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
        self._attr_name = entity_config.get("name", f"Tank {channel}")
        self._attr_native_unit_of_measurement = entity_config.get("unit", "%")
        self._attr_native_value = None
        self._attr_available = True

        self._voltage: float | None = None
        self._cal_points: list = [[0.0, 0], [3.3, 100]]
        self._cal_kind: str = "linear"

        prefix = f"{MQTT_TOPIC_PREFIX}/{device_id}/sensor/{channel}"
        self._voltage_topic = f"{prefix}_voltage/state"
        self._config_topic  = f"{prefix}_config/state"
        self._kind_topic    = f"{prefix}_kind/state"
        self._status_topic  = f"{MQTT_TOPIC_PREFIX}/{device_id}/status"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_config.get("name", f"SmartVan.io {self._device_id}"),
            manufacturer=MANUFACTURER,
            model=self._device_config.get("model", "Unknown"),
            sw_version=self._device_config.get("firmware", "Unknown"),
        )

    @staticmethod
    def _interpolate(voltage: float, points: list, kind: str = "linear") -> float | None:
        if not points or len(points) < 2:
            return None
        pts = sorted(points, key=lambda p: p[0])
        xs = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        # scipy kind strings: linear, nearest, zero, slinear, quadratic, cubic
        # Clamp voltage to calibration range before interpolating
        v = float(np.clip(voltage, xs[0], xs[-1]))
        try:
            fn = interp1d(xs, ys, kind=kind, bounds_error=False, fill_value=(ys[0], ys[-1]))
            return round(float(fn(v)), 1)
        except (ValueError, NotImplementedError):
            # Fall back to linear if kind requires more points than available
            fn = interp1d(xs, ys, kind="linear", bounds_error=False, fill_value=(ys[0], ys[-1]))
            return round(float(fn(v)), 1)

    def _recompute(self) -> None:
        if self._voltage is not None:
            self._attr_native_value = self._interpolate(self._voltage, self._cal_points, self._cal_kind)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        @callback
        def _voltage_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._voltage = payload.get("value")
            self._recompute()

        @callback
        def _config_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if "points" in payload:
                self._cal_points = payload["points"]
                self._recompute()

        @callback
        def _kind_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if "value" in payload:
                self._cal_kind = payload["value"]
                self._recompute()

        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._voltage_topic, _voltage_received, qos=MQTT_QOS)
        await mqtt.async_subscribe(self.hass, self._config_topic,  _config_received,  qos=MQTT_QOS)
        await mqtt.async_subscribe(self.hass, self._kind_topic,    _kind_received,    qos=MQTT_QOS)
        await mqtt.async_subscribe(self.hass, self._status_topic,  _status_received,  qos=MQTT_QOS)


class SmartVanTankConfigSensor(SensorEntity):
    """Stores tank calibration points as a JSON string in entity state.

    Mirrors the ESPHome text entity: state is the raw JSON array string
    ``[[v, pct], ...]`` so the Lovelace card reads it via
    ``JSON.parse(hass.states[entity_id].state)``.
    """

    _attr_has_entity_name = True
    _attr_state_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        self._attr_name = entity_config.get("name", f"Cal Points {channel}")
        self._attr_native_value = "[[0.0, 0], [3.3, 100]]"
        self._attr_available = True

        self._state_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/sensor/{channel}/state"
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
        def _config_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if "points" in payload:
                self._attr_native_value = json.dumps(payload["points"])
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic, _config_received, qos=MQTT_QOS)

        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(self.hass, self._status_topic, _status_received, qos=MQTT_QOS)
