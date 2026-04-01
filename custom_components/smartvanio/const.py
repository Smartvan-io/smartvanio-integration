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

# Platforms we support
PLATFORMS = ["light", "switch", "sensor", "binary_sensor", "scene", "number", "select", "button"]

# Discovery
DISCOVERY_TOPIC_SUFFIX = "config"
STATUS_TOPIC_SUFFIX = "status"

# Entity type mapping
ENTITY_TYPE_LIGHT = "light"
ENTITY_TYPE_SWITCH = "switch"
ENTITY_TYPE_SENSOR = "sensor"
ENTITY_TYPE_BINARY_SENSOR = "binary_sensor"
ENTITY_TYPE_SCENE = "scene"
ENTITY_TYPE_NUMBER = "number"
ENTITY_TYPE_SELECT = "select"
ENTITY_TYPE_BUTTON = "button"

# Scenes MQTT
SCENES_TOPIC_SUFFIX = "scenes"
