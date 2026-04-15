"""SmartVan.io Sensor platform.

Creates Home Assistant sensor entities from SmartVan.io device discovery.
Handles tank levels (water, gas, waste) and any other numeric sensors
declared with entity type "sensor" in the config payload.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MQTT_TOPIC_PREFIX, MQTT_QOS, ENTITY_TYPE_SENSOR

_LOGGER = logging.getLogger(__name__)


def _lerp(xs: list[float], ys: list[float], v: float) -> float:
    """Piecewise linear interpolation."""
    if v <= xs[0]:
        return ys[0]
    if v >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= v <= xs[i + 1]:
            t = (v - xs[i]) / (xs[i + 1] - xs[i]) if xs[i + 1] != xs[i] else 0.0
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def _poly_interp(xs: list[float], ys: list[float], v: float, degree: int = 2) -> float:
    """Local polynomial interpolation (Lagrange basis) of given degree.

    Picks the nearest ``degree + 1`` points around *v* and evaluates the
    Lagrange interpolating polynomial at *v*.  Falls back to linear if
    there aren't enough unique x-values.
    """
    n = degree + 1
    if len(xs) < n:
        return _lerp(xs, ys, v)

    # Find the segment that brackets v
    idx = 0
    for i in range(len(xs) - 1):
        if xs[i] <= v:
            idx = i

    # Centre the window of n points around idx
    start = max(0, min(idx - (n // 2 - 1), len(xs) - n))
    wx = xs[start : start + n]
    wy = ys[start : start + n]

    # Lagrange interpolation
    result = 0.0
    for i in range(n):
        basis = 1.0
        for j in range(n):
            if i != j:
                denom = wx[i] - wx[j]
                if abs(denom) < 1e-12:
                    return _lerp(xs, ys, v)
                basis *= (v - wx[j]) / denom
        result += wy[i] * basis
    return result


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
            elif entity_config.get("sensor_type") == "resistive_interp":
                sensors.append(SmartVanResistiveInterpSensor(hass, device_id, channel, entity_config, config))
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

        # ESPHome publishes entity state on native topics (device_id as prefix)
        self._state_topic = f"{device_id}/sensor/{channel}/state"
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
                    self._attr_native_value = payload.get("value")
                    if "unit" in payload:
                        self._attr_native_unit_of_measurement = payload["unit"]
                else:
                    # ESPHome publishes plain number strings
                    self._attr_native_value = float(raw)
            except (json.JSONDecodeError, ValueError):
                try:
                    self._attr_native_value = float(raw)
                except (ValueError, TypeError):
                    self._attr_native_value = raw
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

        self._state_topic = f"{device_id}/sensor/{channel}/state"
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
                    self._attr_native_value = payload.get("value")
                else:
                    self._attr_native_value = str(raw)
            except (json.JSONDecodeError, ValueError):
                self._attr_native_value = str(raw)
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

        prefix = f"{device_id}/sensor/{channel}"
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
        """Interpolate voltage against calibration points using pure Python.

        Supports: linear, slinear (same as linear), quadratic, cubic.
        Falls back to linear if insufficient points for requested kind.
        """
        if not points or len(points) < 2:
            return None
        pts = sorted(points, key=lambda p: (float(p[0]), float(p[1])))
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]

        # Clamp voltage to calibration range
        v = max(xs[0], min(voltage, xs[-1]))

        # All kinds fall through to linear for < 3 points
        if kind in ("quadratic", "cubic") and len(xs) < 3:
            kind = "linear"
        if kind == "cubic" and len(xs) < 4:
            kind = "quadratic" if len(xs) >= 3 else "linear"

        if kind in ("linear", "slinear", "nearest", "zero"):
            return round(_lerp(xs, ys, v), 1)
        elif kind == "quadratic":
            return round(_poly_interp(xs, ys, v, degree=2), 1)
        elif kind == "cubic":
            return round(_poly_interp(xs, ys, v, degree=3), 1)
        return round(_lerp(xs, ys, v), 1)

    def _recompute(self) -> None:
        if self._voltage is not None:
            self._attr_native_value = self._interpolate(self._voltage, self._cal_points, self._cal_kind)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        @callback
        def _voltage_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    self._voltage = payload.get("value")
                else:
                    self._voltage = float(raw)
            except (json.JSONDecodeError, ValueError):
                try:
                    self._voltage = float(raw)
                except (ValueError, TypeError):
                    return
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
            raw = msg.payload
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and "value" in payload:
                    self._cal_kind = payload["value"]
                else:
                    self._cal_kind = str(raw)
            except (json.JSONDecodeError, ValueError):
                self._cal_kind = str(raw)
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


class SmartVanResistiveInterpSensor(SensorEntity):
    """Interpolated value for a resistive sensor, computed HA-side.

    The firmware config payload provides explicit channel references:
      - ``raw_channel``    — e.g. ``sensor_1_raw``
      - ``points_channel`` — e.g. ``sensor_1_interpolation_points``
      - ``kind_channel``   — e.g. ``sensor_1_interpolation_kind``

    MQTT topics follow ESPHome's native layout:
      - ``{device_id}/sensor/{raw_channel}/state``   — raw voltage (plain float)
      - ``{device_id}/text/{points_channel}/state``   — JSON array ``[[v, mapped], ...]``
      - ``{device_id}/select/{kind_channel}/state``   — interpolation kind string
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
        self._attr_name = entity_config.get("name", f"Sensor {channel}")
        self._attr_native_unit_of_measurement = entity_config.get("unit", None)
        self._attr_native_value = None
        self._attr_available = True

        self._voltage: float | None = None
        self._cal_points: list = [[0.0, 0], [3.3, 100]]
        self._cal_kind: str = "linear"

        raw_ch = entity_config.get("raw_channel", f"{channel}_raw")
        pts_ch = entity_config.get("points_channel", f"{channel}_interpolation_points")
        kind_ch = entity_config.get("kind_channel", f"{channel}_interpolation_kind")

        self._voltage_topic = f"{device_id}/sensor/{raw_ch}/state"
        self._points_topic  = f"{device_id}/text/{pts_ch}/state"
        self._kind_topic    = f"{device_id}/select/{kind_ch}/state"
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "calibration_points": self._cal_points,
            "calibration_kind": self._cal_kind,
            "raw_voltage": self._voltage,
        }

    def _recompute(self) -> None:
        if self._voltage is not None:
            self._attr_native_value = SmartVanTankLevelSensor._interpolate(
                self._voltage, self._cal_points, self._cal_kind,
            )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        @callback
        def _voltage_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            try:
                self._voltage = float(raw)
            except (ValueError, TypeError):
                return
            self._recompute()

        @callback
        def _points_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    self._cal_points = parsed
                    self._recompute()
            except (json.JSONDecodeError, ValueError):
                pass

        @callback
        def _kind_received(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload
            kind = str(raw).strip()
            if kind:
                self._cal_kind = kind
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
        await mqtt.async_subscribe(self.hass, self._points_topic,  _points_received,  qos=MQTT_QOS)
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

        self._state_topic = f"{device_id}/sensor/{channel}/state"
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
