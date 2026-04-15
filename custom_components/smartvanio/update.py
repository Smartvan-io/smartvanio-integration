"""SmartVan.io Update platform.

Exposes firmware update entities for SmartVan.io devices. Checks a GitHub-hosted
manifest per device type to determine if a newer firmware is available, and
triggers OTA flashing via MQTT when the user installs.

Manifest URL pattern:
  https://raw.githubusercontent.com/{org}/{type}.bin/refs/heads/{branch}/manifest.json

Manifest format:
  {"version": "1.1.0", "release_notes": "Fixed calibration persistence"}

OTA trigger:
  Publishes to {device_name}/ota/update with {"url": "...", "md5_url": "..."}
  The device subscribes to this topic and flashes the provided URL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    MQTT_TOPIC_PREFIX,
    MQTT_QOS,
    CONF_BETA_CHANNEL,
    DEFAULT_BETA_CHANNEL,
    FIRMWARE_GITHUB_ORG,
    FIRMWARE_MANIFEST_FILENAME,
)

_LOGGER = logging.getLogger(__name__)


def _build_manifest_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"refs/heads/{branch}/{FIRMWARE_MANIFEST_FILENAME}"
    )


def _build_firmware_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://github.com/{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"raw/refs/heads/{branch}/firmware.bin"
    )


def _build_md5_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"refs/heads/{branch}/hash.txt"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io update entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: dict[str, SmartVanUpdate] = {}

    def _create_update_from_config(
        device_id: str, config: dict
    ) -> list[SmartVanUpdate]:
        firmware_type = config.get("firmware_type")
        if not firmware_type:
            return []

        unique_id = f"{device_id}_firmware"
        if unique_id in created_entities:
            # Update current version if device reported a new one
            existing = created_entities[unique_id]
            new_ver = config.get("firmware")
            if new_ver and new_ver != existing._attr_installed_version:
                existing._attr_installed_version = new_ver
                existing.async_write_ha_state()
            return []

        entity = SmartVanUpdate(hass, entry, device_id, config)
        created_entities[unique_id] = entity
        _LOGGER.info("Created update entity: %s (type=%s)", unique_id, firmware_type)
        return [entity]

    for device_id, config in store.get("pending_configs", {}).items():
        entities = _create_update_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    @callback
    def _on_device_discovered(event) -> None:
        device_id = event.data.get("device_id")
        config = event.data.get("config", {})
        entities = _create_update_from_config(device_id, config)
        if entities:
            async_add_entities(entities)

    hass.bus.async_listen(f"{DOMAIN}_device_discovered", _on_device_discovered)


class SmartVanUpdate(UpdateEntity):
    """Firmware update entity for a SmartVan.io device."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._device_config = device_config
        self._firmware_type = device_config.get("firmware_type", "")

        self._attr_unique_id = f"{device_id}_firmware"
        self._attr_name = "Firmware"
        self._attr_installed_version = device_config.get("firmware")
        self._attr_latest_version = None
        self._attr_available = True
        self._release_notes: str | None = None

        self._ota_topic = f"{device_id}/ota/update"
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
    def _branch(self) -> str:
        beta = self._entry.data.get(CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL)
        return "beta" if beta else "main"

    async def async_added_to_hass(self) -> None:
        @callback
        def _status_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            self._attr_available = payload.get("state") == "online"
            self.async_write_ha_state()

        await mqtt.async_subscribe(
            self.hass, self._status_topic, _status_received, qos=MQTT_QOS
        )

        # Fetch manifest on startup
        await self._fetch_manifest()

    async def async_update(self) -> None:
        """Poll for new firmware version (called by HA periodically)."""
        await self._fetch_manifest()

    async def _fetch_manifest(self) -> None:
        if not self._firmware_type:
            return

        url = _build_manifest_url(self._firmware_type, self._branch)
        session = async_get_clientsession(self.hass)

        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Manifest fetch failed for %s: HTTP %s",
                        self._firmware_type, resp.status,
                    )
                    return
                data = await resp.json(content_type=None)
        except Exception:
            _LOGGER.debug("Failed to fetch manifest for %s", self._firmware_type)
            return

        latest = data.get("version")
        if latest:
            self._attr_latest_version = latest
        self._release_notes = data.get("release_notes")

    def release_notes(self) -> str | None:
        parts = []
        if self._release_notes:
            parts.append(self._release_notes)
        parts.append(f"Channel: **{self._branch}**")
        parts.append(f"Device type: `{self._firmware_type}`")
        return "\n\n".join(parts)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Trigger OTA update on the device via MQTT."""
        branch = self._branch
        fw_url = _build_firmware_url(self._firmware_type, branch)
        md5_url = _build_md5_url(self._firmware_type, branch)

        payload = json.dumps({"url": fw_url, "md5_url": md5_url})

        _LOGGER.info(
            "Triggering OTA update for %s from %s",
            self._device_id, fw_url,
        )

        await mqtt.async_publish(
            self.hass, self._ota_topic, payload,
            qos=MQTT_QOS, retain=False,
        )
