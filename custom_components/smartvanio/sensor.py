"""Support for esphome sensors."""

from __future__ import annotations
from scipy.interpolate import interp1d

from datetime import date, datetime
import json
import logging
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
    async_track_entity_registry_updated_event,
)

from homeassistant.util import dt as dt_util
from homeassistant.util.enum import try_parse_enum

from .entity import EsphomeEntity, platform_async_setup_entry
from .enum_mapper import EsphomeEnumMapper
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up esphome sensors based on a config entry."""

    print("SETTING UP SENSOR")
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

    device_type = entry.data.get("device_type", "")
    if device_type == "smartvanio.resistive_sensor":
        # Create the calibrated sensor
        # This is the raw ESPHome sensor ID
        interpolated_sensor = SmartVanInterpolatedSensor(
            hass,
            entry,
            "sensor_1",
            device_info=entry.data.get("device_info"),
        )
        interpolation_points = SmartVanInterpolationPointsEntity(
            hass,
            entry,
            "sensor_1",
            device_info=entry.data.get("device_info"),
        )
        async_add_entities([interpolated_sensor, interpolation_points])

        # Register calibration service
        # async_register_calibration_service(hass)

    async def set_calibration_data(call: ServiceCall):
        """Handle the service call to update calibration data."""
        new_value = call.data.get("value", 0)
        interpolation_points.set_value(new_value)  # Call method on the sensor

    hass.services.async_register(
        "smartvanio", "set_calibration_data", set_calibration_data
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

    @property
    def native_value(self) -> datetime | str | None:
        """Return the state of the entity."""
        if not self._has_state or (state := self._state).missing_state:
            return None
        state_float = state.state
        if not math.isfinite(state_float):
            return None
        if self.device_class is SensorDeviceClass.TIMESTAMP:
            return dt_util.utc_from_timestamp(state_float)
        return f"{state_float:.{self._static_info.accuracy_decimals}f}"


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

    def __init__(self, hass, entry, sensor_id, device_info):
        """Initialize the calibrated sensor."""
        print("INITIALZED SMARTVAN SENSOR")

        device_prefix = entry.title.replace("-", "_").lower()
        sensor_id_with_prefix = f"{device_prefix}_{sensor_id}"
        self.hass = hass
        self.entry = entry
        self._state = None
        self._device_prefix = device_prefix
        self._sensor_id = f"sensor.{sensor_id_with_prefix}"
        self._name = f"{sensor_id} Interpolated Value"
        self._attr_unique_id = f"{entry.entry_id}_interpolated_value"
        self._device_info = device_info
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, "34:CD:B0:4A:0A:FC")}
        )
        self._attr_calibration = "[[0, 1], [2, 20], [3, 40]]"
        async_track_state_change(
            hass, self._sensor_id, self._async_sensor_state_changed
        )
        async_track_state_change(
            hass,
            f"{self._sensor_id}_interpolation_points",
            self._async_sensor_state_changed,
        )

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        """Return the interpolated sensor value."""
        raw_state = self.hass.states.get(self._sensor_id)

        if raw_state is None or raw_state.state in ["unknown", "unavailable"]:
            return None

        return self._interpolate(raw_state.state)

    @property
    def unit_of_measurement(self):
        return self.entry.data.get("unit", "Custom")

    def _interpolate(self, raw_value):
        """Perform linear interpolation using calibration data."""
        interpolation_points = self.hass.states.get(
            "sensor.smartvanio_rs_4a0afc_sensor_1_interpolation_points"
        ).state

        if not interpolation_points:
            print("Calibration data is empty")
            return raw_value

        points = json.loads(interpolation_points)

        sorted_points = sorted(points, key=lambda x: x[0])
        x_vals, y_vals = zip(*sorted_points, strict=False)  # Unzip into separate lists

        interpolator = interp1d(
            x_vals,
            y_vals,
            kind="linear",
            fill_value="extrapolate",  # Handle clamping internally
        )

        # Calculate interpolated value
        interpolated = interpolator(raw_value)

        # Clamp to min/max y-values (for methods that might overshoot)
        y_min = min(y_vals)
        y_max = max(y_vals)
        return max(min(interpolated, y_max), y_min)

    async def _async_sensor_state_changed(self, entity_id, old_state, new_state):
        """Handle state changes of the raw sensor."""
        if new_state is None or new_state.state in ["unknown", "unavailable"]:
            return

        # Force a state update
        self.async_schedule_update_ha_state()


class SmartVanInterpolationPointsEntity(SensorEntity):
    def __init__(self, hass, entry, sensor_id, device_info):
        """Initialize the calibrated sensor."""
        print("INITIALZED SMARTVAN INTERPOLATION", f"{sensor_id}_interpolation_points")

        device_prefix = entry.title.replace("-", "_").lower()
        sensor_id_with_prefix = f"{device_prefix}_{sensor_id}"

        self.hass = hass
        self.entry = entry
        self._state = None
        self._sensor_id = f"{sensor_id_with_prefix}_interpolation_points"
        self._name = f"{sensor_id} Interpolation points"
        self._attr_unique_id = f"{entry.entry_id}_interpolation_points"
        self._device_info = device_info
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, "34:CD:B0:4A:0A:FC")}
        )
        self._state = "[[0, 1], [2, 20], [3, 40]]"

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    def set_value(self, new_value):
        """Update the sensor state."""
        self._state = new_value
        self.async_write_ha_state()
