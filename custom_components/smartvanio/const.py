"""Constants for the SmartVan.io integration."""

DOMAIN = "smartvanio"
MANUFACTURER = "SmartVan.io"

# MQTT
MQTT_TOPIC_PREFIX = "smartvanio"
MQTT_QOS = 1

# Config keys
CONF_DEVICE_ID = "device_id"
CONF_MQTT_PREFIX = "mqtt_prefix"

# Defaults
DEFAULT_MQTT_PREFIX = "smartvanio"
CONF_BETA_CHANNEL = "beta_channel"
DEFAULT_BETA_CHANNEL = True

# Firmware update
FIRMWARE_GITHUB_ORG = "Smartvan-io"
FIRMWARE_MANIFEST_FILENAME = "manifest.json"

# Platforms we support
PLATFORMS = ["light", "switch", "sensor", "binary_sensor", "number", "select", "button", "update"]

# Discovery
DISCOVERY_TOPIC_SUFFIX = "config"
STATUS_TOPIC_SUFFIX = "status"

# Entity type mapping
ENTITY_TYPE_LIGHT = "light"
ENTITY_TYPE_SWITCH = "switch"
ENTITY_TYPE_SENSOR = "sensor"
ENTITY_TYPE_BINARY_SENSOR = "binary_sensor"
ENTITY_TYPE_NUMBER = "number"
ENTITY_TYPE_SELECT = "select"
ENTITY_TYPE_BUTTON = "button"

# BLE Provisioning
BLE_SERVICE_UUID = "5ac30000-5c6b-4a6e-9a25-736f6e696f00"
BLE_MQTT_CONFIG_CHAR_UUID = "5ac30001-5c6b-4a6e-9a25-736f6e696f00"
BLE_PROV_STATE_CHAR_UUID = "5ac30002-5c6b-4a6e-9a25-736f6e696f00"
