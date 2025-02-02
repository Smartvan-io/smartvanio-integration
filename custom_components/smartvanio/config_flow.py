"""Config flow to configure esphome component."""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any
import voluptuous as vol
import json

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    DeviceInfo,
    InvalidEncryptionKeyAPIError,
    RequiresEncryptionAPIError,
    ResolveAPIError,
)

from homeassistant.components import zeroconf
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_registry import async_get

from .const import (
    CONF_ALLOW_SERVICE_CALLS,
    CONF_DEVICE_NAME,
    CONF_NOISE_PSK,
    DEFAULT_ALLOW_SERVICE_CALLS,
    DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS,
    DOMAIN,
    SENSOR_TYPES,
)

ERROR_REQUIRES_ENCRYPTION_KEY = "requires_encryption_key"
ERROR_INVALID_ENCRYPTION_KEY = "invalid_psk"
SMARTVANIO_URL = "https://www.smartvan.io"
_LOGGER = logging.getLogger(__name__)

ZERO_NOISE_PSK = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class EsphomeFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a esphome config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._host: str | None = None
        self._port: int | None = None
        self._device_info: DeviceInfo | None = None
        self._password: str | None = None
        self._noise_required: bool | None = None
        self._noise_psk: str | None = None
        # The ESPHome name as per its config
        self._device_name: str | None = None

    async def _async_step_user_base(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = 6053

            return await self._async_try_fetch_device_info()

        fields: dict[Any, type] = OrderedDict()
        fields[vol.Required(CONF_HOST, default=self._host or vol.UNDEFINED)] = str
        # fields[vol.Optional(CONF_PORT, default=self._port or 6053)] = int

        errors = {}
        if error is not None:
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"smartvanio_url": SMARTVANIO_URL},
        )

    async def async_step_resistive_sensor(self, user_input=None):
        """Step 2: Configure the resistive sensor module."""
        errors = {}

        if user_input is not None:
            print("Configured Resistive Sensor: %s", self.device_id)
            return self.async_create_entry(
                title=self.device_id,
                data={
                    "device": self.device_id,
                    "device_type": "smartvanio.resistive_sensor",
                    "sensor_1": {
                        "type": user_input["sensor_1_type"],
                        "name": user_input["sensor_1_name"],
                        "unit": user_input["sensor_1_unit"],
                        "calibration": [
                            (
                                user_input["calibration_1_raw"],
                                user_input["calibration_1_value"],
                            ),
                            (
                                user_input["calibration_2_raw"],
                                user_input["calibration_2_value"],
                            ),
                        ],
                    },
                    "sensor_2": {
                        "type": user_input["sensor_2_type"],
                        "name": user_input["sensor_2_name"],
                        "unit": user_input["sensor_2_unit"],
                        "calibration": [
                            (
                                user_input["calibration_2_1_raw"],
                                user_input["calibration_2_1_value"],
                            ),
                            (
                                user_input["calibration_2_2_raw"],
                                user_input["calibration_2_2_value"],
                            ),
                        ],
                    },
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required("sensor_1_type", default="water_tank"): vol.In(
                    SENSOR_TYPES
                ),
                vol.Required("sensor_1_name", default="Sensor 1"): str,
                vol.Required("sensor_1_unit", default="%"): str,
                vol.Required("calibration_1_raw", default=0): vol.Coerce(float),
                vol.Required("calibration_1_value", default=0): vol.Coerce(float),
                vol.Required("calibration_2_raw", default=4095): vol.Coerce(float),
                vol.Required("calibration_2_value", default=100): vol.Coerce(float),
                vol.Required("sensor_2_type", default="temperature"): vol.In(
                    SENSOR_TYPES
                ),
                vol.Required("sensor_2_name", default="Sensor 2"): str,
                vol.Required("sensor_2_unit", default="°C"): str,
                vol.Required("calibration_2_1_raw", default=0): vol.Coerce(float),
                vol.Required("calibration_2_1_value", default=0): vol.Coerce(float),
                vol.Required("calibration_2_2_raw", default=4095): vol.Coerce(float),
                vol.Required("calibration_2_2_value", default=100): vol.Coerce(float),
            }
        )

        return self.async_show_form(
            step_id="resistive_sensor", data_schema=data_schema, errors=errors
        )

    async def async_step_inclinometer(self, user_input=None):
        """Step 2: Configure the inclinometer."""
        _LOGGER.warning("SmartVan: Entered async_step_inclinometer")

        errors = {}

        if user_input is not None:
            print("async_step_inclinometer, self.__dict__")
            print(json.dumps(self.__dict__, indent=2, default=str))

            return self.async_create_entry(
                title=user_input["inclinometer_name"],
                data={
                    "device": self._device_name,
                    "device_type": "smartvanio.inclinometer",
                    "name": user_input["inclinometer_name"],
                    "host": self._host,
                    "port": self._port,
                    "password": self._password,
                    "noise_psk": self._noise_psk,
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required("inclinometer_name", default="Inclinometer"): str,
            }
        )

        _LOGGER.warning("SmartVan: Displaying inclinometer config form")

        return self.async_show_form(
            step_id="inclinometer", data_schema=data_schema, errors=errors
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        print("LOCATED: async_step_user")
        return await self._async_step_user_base(user_input=user_input)

    @property
    def _name(self) -> str | None:
        return self.context.get(CONF_NAME)

    @_name.setter
    def _name(self, value: str) -> None:
        self.context[CONF_NAME] = value
        self.context["title_placeholders"] = {"name": self._name}

    async def _async_try_fetch_device_info(self) -> ConfigFlowResult:
        """Try to fetch device info and return any errors."""
        print("LOCATED: _async_try_fetch_device_info")
        response: str | None
        # After 2024.08, stop trying to fetch device info without encryption
        # so we can avoid probe requests to check for password. At this point
        # most devices should announce encryption support and password is
        # deprecated and can be discovered by trying to connect only after they
        # interact with the flow since it is expected to be a rare case.
        response = await self.fetch_device_info()

        project_name = self._device_info.project_name

        if response is not None:
            return await self._async_step_user_base(error=response)

        if project_name == "smartvanio.inclinometer":
            return await self.async_step_inclinometer()

        return self._async_get_entry()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-confirmation of discovered node."""
        print("located: async_step_discovery_confirm")
        if user_input is not None:
            print("located: async_step_discovery_confirm - user_input not none")
            print(json.dumps(user_input.__dict__, indent=2, default=str))
            return await self._async_try_fetch_device_info()
        return self.async_show_form(
            step_id="discovery_confirm", description_placeholders={"name": self._name}
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        print("located: async_step_zeroconf")
        """Handle zeroconf discovery."""
        mac_address: str | None = discovery_info.properties.get("mac")

        # Mac address was added in Sept 20, 2021.
        # https://github.com/esphome/esphome/pull/2303
        if mac_address is None:
            return self.async_abort(reason="mdns_missing_mac")

        # mac address is lowercase and without :, normalize it
        mac_address = format_mac(mac_address)

        # Hostname is format: livingroom.local.
        device_name = discovery_info.hostname.removesuffix(".local.")

        self._name = discovery_info.properties.get("friendly_name", device_name)
        self._device_name = device_name
        self._host = discovery_info.host
        self._port = discovery_info.port
        self._noise_required = bool(discovery_info.properties.get("api_encryption"))

        # Check if already configured
        await self.async_set_unique_id(mac_address)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: self._port}
        )

        return await self.async_step_discovery_confirm()

    @callback
    def _async_get_entry(self) -> ConfigFlowResult:
        config_data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            # The API uses protobuf, so empty string denotes absence
            CONF_PASSWORD: self._password or "",
            CONF_NOISE_PSK: self._noise_psk or "",
            CONF_DEVICE_NAME: self._device_name,
        }
        config_options = {
            CONF_ALLOW_SERVICE_CALLS: DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS,
        }
        print("located: _async_get_entry")

        assert self._name is not None
        return self.async_create_entry(
            title=self._name,
            data=config_data,
            options=config_options,
        )

    async def fetch_device_info(self) -> str | None:
        """Fetch device info from API and return any errors."""
        print("located: fetch_device_info")
        zeroconf_instance = await zeroconf.async_get_instance(self.hass)
        assert self._host is not None
        assert self._port is not None
        cli = APIClient(
            self._host,
            self._port,
            "",
            zeroconf_instance=zeroconf_instance,
            noise_psk=self._noise_psk,
        )

        try:
            await cli.connect()
            self._device_info = await cli.device_info()
        except ResolveAPIError:
            return "resolve_error"
        except APIConnectionError:
            return "connection_error"
        finally:
            await cli.disconnect(force=True)

        self._name = self._device_info.friendly_name or self._device_info.name
        self._device_name = self._device_info.name
        mac_address = format_mac(self._device_info.mac_address)
        await self.async_set_unique_id(mac_address, raise_on_progress=False)

        return None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handle a option flow for esphome."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ALLOW_SERVICE_CALLS,
                    default=self.config_entry.options.get(
                        CONF_ALLOW_SERVICE_CALLS, DEFAULT_ALLOW_SERVICE_CALLS
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)
