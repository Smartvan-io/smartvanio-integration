"""Config flow for SmartVan.io integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MQTT_PREFIX, default=DEFAULT_MQTT_PREFIX): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input.

    Check that MQTT is available and the prefix is valid.
    """
    # Verify MQTT integration is loaded
    if "mqtt" not in hass.config.components:
        raise MqttNotAvailable

    prefix = data.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
    if not prefix or "/" in prefix or "#" in prefix or "+" in prefix:
        raise InvalidPrefix

    return {"title": f"SmartVan.io ({prefix})"}


class SmartVanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SmartVan.io."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user adds the integration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except MqttNotAvailable:
                errors["base"] = "mqtt_not_available"
            except InvalidPrefix:
                errors["base"] = "invalid_prefix"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Prevent duplicate entries
                await self.async_set_unique_id(
                    user_input.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class MqttNotAvailable(HomeAssistantError):
    """Error to indicate MQTT is not configured."""


class InvalidPrefix(HomeAssistantError):
    """Error to indicate the MQTT prefix is invalid."""
