"""Support for esphome sensors."""

from __future__ import annotations

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

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util.enum import try_parse_enum

from .entity import EsphomeEntity, platform_async_setup_entry
from .enum_mapper import EsphomeEnumMapper

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up esphome sensors based on a config entry."""

    print("SETTING UP SENSOR")
    print(json.dumps(entry.data, indent=2, default=str))

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
    print(device_type)
    if device_type == "smartvanio.resistive_sensor":
        print("SETTING UP resistive SENSOR")
        _LOGGER.info(f"Setting up SmartVanCalibratedSensor for {entry.title}")

        # Get the raw sensor entity ID
        raw_sensor_id = entry.data.get("raw_sensor_id", "sensor.raw_sensor_value")

        # Load calibration data
        calibration_data = json.loads(entry.data.get("calibration", "[]"))

        # Create the calibrated sensor
        calibrated_sensor = SmartVanCalibratedSensor(
            hass, entry, raw_sensor_id, calibration_data
        )
        async_add_entities([calibrated_sensor])

        # Register calibration service
        # async_register_calibration_service(hass)


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


class SmartVanCalibratedSensor(SensorEntity):
    """A sensor that applies calibration to a raw ESPHome sensor value."""

    def __init__(self, hass, entry, raw_sensor_id, calibration_data):
        """Initialize the calibrated sensor."""
        print("INITIALZED SMARTVAN SENSOR")
        print(raw_sensor_id)
        self.hass = hass
        self.entry = entry
        self._state = None
        self._raw_sensor_id = raw_sensor_id  # This is the raw ESPHome sensor ID
        self._calibration_data = "[]"
        self._name = f"{entry.title} Calibrated Sensor"
        self._attr_unique_id = f"{entry.entry_id}_calibration_data"
        self._attr_device_info = DeviceInfo(
            identifiers={("smartvanio", "smartvanio-rs 4a0afc")},
            manufacturer="smartvanio",
            model="resistive_sensor",
            name="smartvanio-rs 4a0afc",
        )

    @property
    def name(self):
        print("NAME %s", self._name)
        return self._name

    @property
    def state(self):
        """Return the interpolated sensor value."""
        raw_state = self.hass.states.get(self._raw_sensor_id)
        if raw_state is None or raw_state.state in ["unknown", "unavailable"]:
            return None

        try:
            raw_value = float(raw_state.state)
        except ValueError:
            return None

        return self._interpolate(raw_value)

    @property
    def unit_of_measurement(self):
        return self.entry.data.get("unit", "Custom")

    def _interpolate(self, raw_value):
        """Perform linear interpolation using calibration data."""
        if not self._calibration_data or len(self._calibration_data) < 2:
            return raw_value  # No calibration data, return raw

        sorted_points = sorted(self._calibration_data, key=lambda x: x[0])
        for i in range(len(sorted_points) - 1):
            x1, y1 = sorted_points[i]
            x2, y2 = sorted_points[i + 1]
            if x1 <= raw_value <= x2:
                return y1 + (raw_value - x1) * (y2 - y1) / (x2 - x1)

        return raw_value  # Default to raw if out of range

    async def async_update_calibration(self, calibration_data):
        """Update calibration data and store it in the config entry."""
        self._calibration_data = calibration_data

        # Update the stored calibration data in the integration
        updated_data = {**self.entry.data, "calibration": json.dumps(calibration_data)}
        self.hass.config_entries.async_update_entry(self.entry, data=updated_data)

        self.async_schedule_update_ha_state()
