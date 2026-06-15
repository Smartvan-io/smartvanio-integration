"""Repair flows for the SmartVan.io integration.

When a known device drops offline and reappears in BLE setup mode, __init__.py
raises a `reprovision_<device_id>` repair issue. Its fix flow re-launches the
Bluetooth provisioning config flow for that device so the user can give it WiFi
+ MQTT details again — without deleting the device.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class ReprovisionRepairFlow(RepairsFlow):
    """Confirm, then re-launch BLE provisioning for an offline device."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data or {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            placeholders = {"name": self._data.get("name", self._data.get("device_id", ""))}
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
            )

        # Pull the most recent advertisement for this device so we hand the
        # config flow a fresh, connectable BLEDevice rather than a stale one.
        address = self._data.get("ble_address")
        service_info = None
        if address:
            service_info = bluetooth.async_last_service_info(
                self.hass, address, connectable=True
            )

        if service_info is not None:
            # Kick off the normal Bluetooth provisioning flow; the user then
            # completes the WiFi/MQTT form in the discovered device card.
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_BLUETOOTH},
                    data=service_info,
                )
            )

        # Resolve the repair regardless — if the device wasn't in range, the
        # issue will be re-raised on its next advertisement.
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for a SmartVan.io repair issue."""
    return ReprovisionRepairFlow(data)
