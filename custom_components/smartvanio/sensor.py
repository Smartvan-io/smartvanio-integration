"""Support for esphome sensors."""

from __future__ import annotations
from typing import Type
from scipy.interpolate import interp1d

from datetime import date, datetime
import json
import math

from aioesphomeapi import (
    EntityInfo,
    SensorInfo,
    SensorState,
    SensorStateClass as EsphomeSensorStateClass,
    TextSensorInfo,
    TextSensorState,
)
from aioesphomeapi.model import LastResetType
from config.custom_components.smartvanio.entry_data import RuntimeEntryData
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change,
    async_track_state_change_event,
)

from homeassistant.util import dt as dt_util
from homeassistant.util.enum import try_parse_enum

from .entity import EsphomeEntity, platform_async_setup_entry
from .enum_mapper import EsphomeEnumMapper


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up esphome sensors based on a config entry."""
    await platform_async_setup_entry(
        hass,
        entry,
        async_add_entities,
        info_type=SensorInfo,
        entity_type=EsphomeSensor,
        state_type=SensorState,
    )
    await platform_async_setup_entry(
        hass,
        entry,
        async_add_entities,
        info_type=TextSensorInfo,
        entity_type=EsphomeTextSensor,
        state_type=TextSensorState,
    )


_STATE_CLASSES: EsphomeEnumMapper[EsphomeSensorStateClass, SensorStateClass | None] = (
    EsphomeEnumMapper(
        {
            EsphomeSensorStateClass.NONE: None,
            EsphomeSensorStateClass.MEASUREMENT: SensorStateClass.MEASUREMENT,
            EsphomeSensorStateClass.TOTAL_INCREASING: SensorStateClass.TOTAL_INCREASING,
            EsphomeSensorStateClass.TOTAL: SensorStateClass.TOTAL,
        }
    )
)


class EsphomeSensor(EsphomeEntity[SensorInfo, SensorState], SensorEntity):
    """A sensor implementation for esphome."""

    @callback
    def _on_static_info_update(self, static_info: EntityInfo) -> None:
        """Set attrs from static info."""
        super()._on_static_info_update(static_info)
        static_info = self._static_info
        self._attr_force_update = static_info.force_update
        # protobuf doesn't support nullable strings so we need to check
        # if the string is empty
        if unit_of_measurement := static_info.unit_of_measurement:
            self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = try_parse_enum(
            SensorDeviceClass, static_info.device_class
        )
        if not (state_class := static_info.state_class):
            return
        if (
            state_class == EsphomeSensorStateClass.MEASUREMENT
            and static_info.last_reset_type == LastResetType.AUTO
        ):
            # Legacy, last_reset_type auto was the equivalent to the
            # TOTAL_INCREASING state class
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        else:
            self._attr_state_class = _STATE_CLASSES.from_esphome(state_class)

    def __init__(
        self,
        entry_data: RuntimeEntryData,
        domain: str,
        entity_info: EntityInfo,
        state_type: Type[SensorState],
        hass: HomeAssistant,
    ) -> None:
        super().__init__(entry_data, domain, entity_info, state_type, hass)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        @callback
        def _async_sensor_state_changed(event):
            """Handle state changes of the raw sensor."""
            self.async_schedule_update_ha_state()

        if self.entity_id.endswith("sensor_1_interpolated_value"):
            entity_id = self.entity_id.removesuffix("_interpolated_value")
            async_track_state_change_event(
                self.hass,
                [f"{entity_id}_raw", f"{entity_id}_interpolation_points"],
                _async_sensor_state_changed,
            )

    @property
    def native_value(self) -> datetime | str | None:
        """Return the state of the entity."""

        if self.entity_id.endswith("interpolated_value"):
            base_entity_id = self.entity_id.removesuffix(
                "_interpolated_value"
            ).removeprefix("sensor.")

            raw_entity_id = f"sensor.{base_entity_id}_raw"

            interpolation_points_entity_id = (
                f"text.{base_entity_id}_interpolation_points"
            )

            interpolation_kind_entity_id = f"select.{base_entity_id}_interpolation_kind"

            raw_value = self.hass.states.get(raw_entity_id).state

            interpolation_points = self.hass.states.get(
                interpolation_points_entity_id
            ).state

            interpolation_kind = self.hass.states.get(
                interpolation_kind_entity_id
            ).state

            return self._interpolate(
                raw_value, interpolation_points, interpolation_kind
            )

        if not self._has_state or (state := self._state).missing_state:
            return None
        state_float = state.state
        if not math.isfinite(state_float):
            return None
        if self.device_class is SensorDeviceClass.TIMESTAMP:
            return dt_util.utc_from_timestamp(state_float)
        return f"{state_float:.{self._static_info.accuracy_decimals}f}"

    def _interpolate(
        self, raw_value, interpolation_points, interpolation_kind="linear"
    ):
        """Perform linear interpolation using calibration data."""

        if raw_value is None:
            return raw_value

        if not interpolation_points or interpolation_points in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return raw_value

        points = json.loads(interpolation_points)

        if len(points) < 2:
            return raw_value

        sorted_points = sorted(points, key=lambda x: x[0])
        x_vals, y_vals = zip(*sorted_points, strict=False)

        interpolator = interp1d(
            x_vals,
            y_vals,
            kind=interpolation_kind,
            fill_value="extrapolate",  # Handle clamping internally
        )

        # Calculate interpolated value
        interpolated = interpolator(raw_value)

        # Clamp to min/max y-values (for methods that might overshoot)
        y_min = min(y_vals)
        y_max = max(y_vals)
        result = max(min(interpolated, y_max), y_min)

        return (int(10 * result - 0.5) + 1) / 10.0


class EsphomeTextSensor(EsphomeEntity[TextSensorInfo, TextSensorState], SensorEntity):
    """A text sensor implementation for ESPHome."""

    @callback
    def _on_static_info_update(self, static_info: EntityInfo) -> None:
        """Set attrs from static info."""
        super()._on_static_info_update(static_info)
        static_info = self._static_info
        self._attr_device_class = try_parse_enum(
            SensorDeviceClass, static_info.device_class
        )

    @property
    def native_value(self) -> str | datetime | date | None:
        """Return the state of the entity."""
        if not self._has_state or (state := self._state).missing_state:
            return None
        state_str = state.state
        device_class = self.device_class
        if device_class is SensorDeviceClass.TIMESTAMP:
            return dt_util.parse_datetime(state_str)
        if (
            device_class is SensorDeviceClass.DATE
            and (value := dt_util.parse_datetime(state_str)) is not None
        ):
            return value.date()
        return state_str


class SmartVanInterpolatedSensor(SensorEntity):
    """A sensor that applies calibration to a raw ESPHome sensor value."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sensor_id):
        """Initialize the calibrated sensor."""

        device_prefix = entry.title.replace("-", "_").lower()
        sensor_id_with_prefix = f"{device_prefix}_{sensor_id}"
        self.hass = hass
        self.entry = entry
        self._state = None
        self._device_prefix = device_prefix

        self._sensor = sensor_id
        self._sensor_id = f"sensor.{sensor_id_with_prefix}"
        self._name = f"{sensor_id} Interpolated Value"
        self._attr_unique_id = f"{self._sensor_id}_interpolated_value"
        self.entity_id = f"sensor.{sensor_id_with_prefix}_interpolated_value"

        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, entry.data.get("mac_address"))}
        )
        self.device_class = "measurement"
        self._attr_device_class = "measurement"
        self._attr_unit_of_measurement = "%"
        async_track_state_change(
            hass, self._sensor_id, self._async_sensor_state_changed
        )

        return None

    @property
    def name(self):
        return self._name

    @property
    def extra_state_attributes(self):
        # Merge data and options
        data = self.entry.data.get(self._sensor, {})
        options = self.entry.options.get(self._sensor, {})

        # Or combine them in some way
        min_resistance = options.get("min_resistance", data.get("min_resistance", 0))
        max_resistance = options.get("max_resistance", data.get("max_resistance", 190))
        interpolation_points = options.get(
            "interpolation_points", data.get("interpolation_points", "[[]]")
        )

        return {
            "min_resistance": min_resistance,
            "max_resistance": max_resistance,
            "interpolation_points": interpolation_points,
            "unit_of_measurement": self._attr_unit_of_measurement,
        }

    @property
    def state(self):
        """Return the interpolated sensor value."""
        raw_state = self.hass.states.get(self._sensor_id)

        if raw_state is None or raw_state.state in ["unknown", "unavailable"]:
            return None

        return self._interpolate(raw_state.state)

    def _interpolate(self, raw_value):
        """Perform linear interpolation using calibration data."""

        if self.hass.states.get(f"{self._sensor_id}_interpolated_value") is None:
            return raw_value

        attributes = self.hass.states.get(
            f"{self._sensor_id}_interpolated_value"
        ).attributes

        interpolation_points = attributes.get("interpolation_points")
        interpolation_kind = attributes.get("interpolation_kind", "linear")

        if not interpolation_points:
            return raw_value

        points = json.loads(interpolation_points)

        if len(points) < 2:
            return raw_value

        sorted_points = sorted(points, key=lambda x: x[0])
        x_vals, y_vals = zip(*sorted_points, strict=False)

        interpolator = interp1d(
            x_vals,
            y_vals,
            kind=interpolation_kind,
            fill_value="extrapolate",  # Handle clamping internally
        )

        # Calculate interpolated value
        interpolated = interpolator(raw_value)

        # Clamp to min/max y-values (for methods that might overshoot)
        y_min = min(y_vals)
        y_max = max(y_vals)
        result = max(min(interpolated, y_max), y_min)

        return (int(10 * result - 0.5) + 1) / 10.0

    async def _async_sensor_state_changed(self, entity_id, old_state, new_state):
        """Handle state changes of the raw sensor."""
        if new_state is None or new_state.state in ["unknown", "unavailable"]:
            return

        # Force a state update
        self.async_schedule_update_ha_state()
