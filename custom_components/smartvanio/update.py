"""SmartVan.io Update platform.

Exposes update entities for:
  - Device firmware (OTA via MQTT)
  - Dashboard card (downloaded from GitHub)
  - Integration itself (cloned from GitHub)

OTA flow:
  1. HA downloads firmware.bin + hash.txt from GitHub
  2. Saves to /config/www/smartvanio/ota/{firmware_type}/
  3. Publishes MQTT to {device_name}/ota/update with local HA URL
  4. Device pulls firmware over plain HTTP from HA on the LAN

OTA progress:
  Device publishes to smartvanio/{device_name}/ota/state with:
    {"state": "downloading|flashing|done|error", "progress": 0-100, "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
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
from homeassistant.helpers.network import get_url

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

OTA_SERVE_DIR = "/config/www/smartvanio/ota"


def _build_manifest_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"refs/heads/{branch}/{FIRMWARE_MANIFEST_FILENAME}"
    )


def _build_github_firmware_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"refs/heads/{branch}/firmware.bin"
    )


def _build_github_md5_url(firmware_type: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{firmware_type}.bin/"
        f"refs/heads/{branch}/hash.txt"
    )


CARD_GITHUB_REPO = "smartvanio-main-card"
CARD_INSTALL_DIR = "/config/www/smartvanio"
CARD_VERSION_FILE = "/config/www/smartvanio/.card_version"

INTEGRATION_GITHUB_REPO = "smartvanio-integration"
INTEGRATION_INSTALL_DIR = "/config/custom_components/smartvanio"
INTEGRATION_MANIFEST = "/config/custom_components/smartvanio/manifest.json"


def _build_card_url(filename: str, branch: str) -> str:
    return (
        f"https://raw.githubusercontent.com/"
        f"{FIRMWARE_GITHUB_ORG}/{CARD_GITHUB_REPO}/"
        f"refs/heads/{branch}/{filename}"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartVan.io update entities from config entry."""
    store = hass.data[DOMAIN][entry.entry_id]
    created_entities: dict[str, SmartVanUpdate] = {}

    # Always create the card and integration update entities
    card_entity = SmartVanCardUpdate(hass, entry)
    integration_entity = SmartVanIntegrationUpdate(hass, entry)
    async_add_entities([card_entity, integration_entity])

    def _create_update_from_config(
        device_id: str, config: dict
    ) -> list[SmartVanUpdate]:
        firmware_type = config.get("firmware_type")
        if not firmware_type:
            return []

        unique_id = f"{device_id}_firmware"
        if unique_id in created_entities:
            existing = created_entities[unique_id]
            new_ver = config.get("firmware")
            if new_ver and new_ver != existing._attr_installed_version:
                existing._attr_installed_version = new_ver
                if existing._attr_in_progress is not False:
                    existing._attr_in_progress = False
                    _LOGGER.info(
                        "OTA complete for %s — now running %s",
                        device_id, new_ver,
                    )
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
        | UpdateEntityFeature.PROGRESS
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
        self._attr_in_progress: bool | int = False
        self._release_notes: str | None = None

        self._ota_topic = f"{device_id}/ota/update"
        self._ota_state_topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/ota/state"
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
            online = payload.get("state") == "online"
            self._attr_available = online
            # Device came back online after OTA — clear progress
            if online and self._attr_in_progress is not False:
                self._attr_in_progress = False
                _LOGGER.info(
                    "OTA %s: device back online after update",
                    self._device_id,
                )
            self.async_write_ha_state()

        await mqtt.async_subscribe(
            self.hass, self._status_topic, _status_received, qos=MQTT_QOS
        )

        @callback
        def _ota_state_received(msg: mqtt.ReceiveMessage) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return

            state = payload.get("state", "")
            progress = payload.get("progress")

            if state in ("downloading", "flashing"):
                self._attr_in_progress = True
            elif state == "done":
                self._attr_in_progress = False
                _LOGGER.info("OTA %s: flash complete, device rebooting", self._device_id)
            elif state == "error":
                self._attr_in_progress = False
                _LOGGER.error("OTA %s failed: %s", self._device_id, payload.get("error", "unknown"))

            self.async_write_ha_state()

        await mqtt.async_subscribe(
            self.hass, self._ota_state_topic, _ota_state_received, qos=MQTT_QOS
        )

        await self._fetch_manifest()

    async def async_update(self) -> None:
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

    async def _download_firmware(self) -> tuple[str, str]:
        """Download firmware from GitHub and save locally for devices to pull."""
        branch = self._branch
        fw_url = _build_github_firmware_url(self._firmware_type, branch)
        md5_url = _build_github_md5_url(self._firmware_type, branch)

        serve_dir = os.path.join(OTA_SERVE_DIR, self._firmware_type)
        os.makedirs(serve_dir, exist_ok=True)

        session = async_get_clientsession(self.hass)

        # Download firmware binary
        fw_path = os.path.join(serve_dir, "firmware.bin")
        async with session.get(fw_url, timeout=120) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to download firmware: HTTP {resp.status}")
            data = await resp.read()
            await self.hass.async_add_executor_job(self._write_file, fw_path, data)
            _LOGGER.info(
                "Downloaded firmware for %s: %d bytes",
                self._firmware_type, len(data),
            )

        # Download hash
        md5_path = os.path.join(serve_dir, "hash.txt")
        async with session.get(md5_url, timeout=15) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to download hash: HTTP {resp.status}")
            data = await resp.read()
            await self.hass.async_add_executor_job(self._write_file, md5_path, data)

        # Build local URLs for devices
        # /local/ maps to /config/www/ in HA
        local_fw = f"/local/smartvanio/ota/{self._firmware_type}/firmware.bin"
        local_md5 = f"/local/smartvanio/ota/{self._firmware_type}/hash.txt"

        return local_fw, local_md5

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    def _get_ha_base_url(self) -> str:
        """Get HA's local network URL for devices to reach."""
        try:
            return get_url(self.hass, prefer_external=False)
        except Exception:
            _LOGGER.warning(
                "Could not determine HA internal URL — "
                "set 'internal_url' in configuration.yaml"
            )
            raise

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download firmware from GitHub, serve locally, trigger OTA via MQTT."""
        self._attr_in_progress = True
        self.async_write_ha_state()

        try:
            # Step 1: Download firmware from GitHub to local www dir
            local_fw_path, local_md5_path = await self._download_firmware()

            # Step 2: Build full URLs using HA's base URL
            base = self._get_ha_base_url()
            fw_url = f"{base}{local_fw_path}"
            md5_url = f"{base}{local_md5_path}"

            _LOGGER.info(
                "Triggering OTA for %s — firmware served at %s",
                self._device_id, fw_url,
            )

            # Step 3: Tell device to pull from HA
            payload = json.dumps({"url": fw_url, "md5_url": md5_url})
            await mqtt.async_publish(
                self.hass, self._ota_topic, payload,
                qos=MQTT_QOS, retain=False,
            )

        except Exception as err:
            _LOGGER.error("OTA failed for %s: %s", self._device_id, err)
            self._attr_in_progress = False
            self.async_write_ha_state()
            return

        # Timeout guard — reset progress if no response in 120s
        async def _timeout_guard():
            await asyncio.sleep(120)
            if self._attr_in_progress is not False:
                _LOGGER.warning(
                    "OTA timeout for %s — no progress in 120s",
                    self._device_id,
                )
                self._attr_in_progress = False
                self.async_write_ha_state()

        self.hass.async_create_task(_timeout_guard())


class SmartVanCardUpdate(UpdateEntity):
    """Update entity for the SmartVan.io dashboard card."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = "smartvanio_card"
        self._attr_name = "Dashboard Card"
        self._attr_installed_version = self._read_installed_version()
        self._attr_latest_version = None
        self._attr_in_progress: bool | int = False
        self._release_notes: str | None = None
        self._attr_entity_picture = "https://smartvan.io/icon.png"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "smartvanio_hub")},
            name="SmartVan.io",
            manufacturer=MANUFACTURER,
            model="Dashboard",
        )

    @property
    def _branch(self) -> str:
        beta = self._entry.data.get(CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL)
        return "beta" if beta else "main"

    @staticmethod
    def _read_installed_version() -> str | None:
        try:
            with open(CARD_VERSION_FILE) as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    @staticmethod
    def _write_installed_version(version: str) -> None:
        os.makedirs(CARD_INSTALL_DIR, exist_ok=True)
        with open(CARD_VERSION_FILE, "w") as f:
            f.write(version)

    async def async_added_to_hass(self) -> None:
        await self._fetch_manifest()

    async def async_update(self) -> None:
        await self._fetch_manifest()

    async def _fetch_manifest(self) -> None:
        url = _build_card_url("package.json", self._branch)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
        except Exception:
            _LOGGER.debug("Failed to fetch card manifest")
            return

        latest = data.get("version")
        if latest:
            self._attr_latest_version = latest

    def release_notes(self) -> str | None:
        parts = []
        if self._release_notes:
            parts.append(self._release_notes)
        parts.append(f"Channel: **{self._branch}**")
        parts.append("Updates the SmartVan.io dashboard card and kiosk mode.")
        return "\n\n".join(parts)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download latest card files from GitHub."""
        self._attr_in_progress = True
        self.async_write_ha_state()

        try:
            session = async_get_clientsession(self.hass)
            branch = self._branch

            os.makedirs(CARD_INSTALL_DIR, exist_ok=True)

            # Download main card
            card_url = _build_card_url("index.js", branch)
            card_path = os.path.join(CARD_INSTALL_DIR, "smartvanio-main-card.js")
            async with session.get(card_url, timeout=60) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to download card: HTTP {resp.status}")
                data = await resp.read()
                await self.hass.async_add_executor_job(
                    self._write_file, card_path, data
                )
                _LOGGER.info("Downloaded smartvanio-main-card.js: %d bytes", len(data))

            # Download kiosk mode
            kiosk_url = _build_card_url("kiosk-mode.js", branch)
            kiosk_path = os.path.join(CARD_INSTALL_DIR, "kiosk-mode.js")
            async with session.get(kiosk_url, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    await self.hass.async_add_executor_job(
                        self._write_file, kiosk_path, data
                    )
                    _LOGGER.info("Downloaded kiosk-mode.js: %d bytes", len(data))

            # Update installed version
            new_version = version or self._attr_latest_version
            if new_version:
                await self.hass.async_add_executor_job(
                    self._write_installed_version, new_version
                )
                self._attr_installed_version = new_version

        except Exception as err:
            _LOGGER.error("Card update failed: %s", err)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)


class SmartVanIntegrationUpdate(UpdateEntity):
    """Update entity for the SmartVan.io integration."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = "smartvanio_integration"
        self._attr_name = "Integration"
        self._attr_installed_version = self._read_installed_version()
        self._attr_latest_version = None
        self._attr_in_progress: bool | int = False
        self._release_notes: str | None = None
        self._attr_entity_picture = "https://smartvan.io/icon.png"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "smartvanio_hub")},
            name="SmartVan.io",
            manufacturer=MANUFACTURER,
            model="Dashboard",
        )

    @property
    def _branch(self) -> str:
        beta = self._entry.data.get(CONF_BETA_CHANNEL, DEFAULT_BETA_CHANNEL)
        return "beta" if beta else "main"

    @staticmethod
    def _read_installed_version() -> str | None:
        try:
            with open(INTEGRATION_MANIFEST) as f:
                data = json.load(f)
                return data.get("version")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    async def async_added_to_hass(self) -> None:
        await self._fetch_manifest()

    async def async_update(self) -> None:
        await self._fetch_manifest()

    async def _fetch_manifest(self) -> None:
        url = (
            f"https://raw.githubusercontent.com/"
            f"{FIRMWARE_GITHUB_ORG}/{INTEGRATION_GITHUB_REPO}/"
            f"refs/heads/{self._branch}/custom_components/smartvanio/manifest.json"
        )
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
        except Exception:
            _LOGGER.debug("Failed to fetch integration manifest")
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
        parts.append("Updates the SmartVan.io integration. Restart HA after installing.")
        return "\n\n".join(parts)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Clone latest integration from GitHub and replace installed files."""
        self._attr_in_progress = True
        self.async_write_ha_state()

        try:
            branch = self._branch
            repo_url = (
                f"https://github.com/{FIRMWARE_GITHUB_ORG}/"
                f"{INTEGRATION_GITHUB_REPO}.git"
            )

            await self.hass.async_add_executor_job(
                self._clone_and_install, repo_url, branch
            )

            new_version = self._read_installed_version()
            if new_version:
                self._attr_installed_version = new_version

            _LOGGER.info(
                "Integration updated to %s — restart HA to apply",
                new_version,
            )

        except Exception as err:
            _LOGGER.error("Integration update failed: %s", err)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

    @staticmethod
    def _clone_and_install(repo_url: str, branch: str) -> None:
        tmp_dir = tempfile.mkdtemp()
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, repo_url, tmp_dir],
                check=True,
                capture_output=True,
            )
            src = os.path.join(tmp_dir, "custom_components", "smartvanio")
            if not os.path.isdir(src):
                raise Exception("Integration not found in cloned repo")

            if os.path.isdir(INTEGRATION_INSTALL_DIR):
                shutil.rmtree(INTEGRATION_INSTALL_DIR)
            shutil.copytree(src, INTEGRATION_INSTALL_DIR)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
