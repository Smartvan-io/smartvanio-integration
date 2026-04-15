#!/bin/bash
# SmartVan.io Setup Add-on
# Installs integration, cards, and dashboard into Home Assistant.
#
# Runs in two modes:
#   1. Supervisor mode (production) — uses bashio + SUPERVISOR_TOKEN
#   2. Standalone mode (dev) — plain bash, file install only

set -e

# ── Detect environment ───────────────────────────────────────

if [ -f /usr/bin/bashio ] && [ -n "$SUPERVISOR_TOKEN" ]; then
    MODE="supervisor"
    # Source bashio for logging helpers
    source /usr/bin/bashio 2>/dev/null || true
    log_info() { bashio::log.info "$@"; }
    log_warn() { bashio::log.warning "$@"; }
    log_error() { bashio::log.error "$@"; }
    get_config() { bashio::config "$1"; }
else
    MODE="standalone"
    log_info() { echo "[INFO]  $*"; }
    log_warn() { echo "[WARN]  $*"; }
    log_error() { echo "[ERROR] $*"; }
    get_config() {
        # Read from /data/options.json if available (Supervisor), otherwise use defaults
        if [ -f /data/options.json ]; then
            jq -r ".$1 // empty" /data/options.json 2>/dev/null
        fi
    }
fi

log_info "============================================="
log_info "  SmartVan.io Setup (mode: ${MODE})"
log_info "============================================="

MQTT_USER=$(get_config 'mqtt_user')
MQTT_PASSWORD=$(get_config 'mqtt_password')
MQTT_USER="${MQTT_USER:-smartvanio}"
MQTT_PASSWORD="${MQTT_PASSWORD:-smartvanio123}"

# ── Helpers ──────────────────────────────────────────────────

ha_api() {
    local method="$1"
    local endpoint="$2"
    local data="$3"

    if [ "$MODE" = "supervisor" ]; then
        local url="http://supervisor/core/api${endpoint}"
        local token="$SUPERVISOR_TOKEN"
    else
        local url="${HA_URL:-http://homeassistant:8123}/api${endpoint}"
        local token="${HA_TOKEN:-}"
    fi

    if [ -z "$token" ]; then
        return 1
    fi

    if [ -n "$data" ]; then
        curl -s -X "$method" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "$url" 2>/dev/null
    else
        curl -s -X "$method" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            "$url" 2>/dev/null
    fi
}

wait_for_ha() {
    log_info "Waiting for Home Assistant API..."
    for i in $(seq 1 60); do
        RESULT=$(ha_api GET "/" 2>/dev/null || echo "")
        if echo "$RESULT" | grep -q "API running"; then
            log_info "  Home Assistant API is ready"
            return 0
        fi
        sleep 2
    done
    log_warn "  Home Assistant API not available"
    return 1
}

# ── Step 1: Install integration ──────────────────────────────

log_info ""
log_info "Step 1/4: Installing SmartVan.io integration..."

INTEGRATION_DIR="/config/custom_components/smartvanio"
SRC_DIR="/opt/smartvanio/integration"

if [ -d "$SRC_DIR" ] && [ "$(ls -A "$SRC_DIR" 2>/dev/null)" ]; then
    mkdir -p "$INTEGRATION_DIR"
    cp -r "$SRC_DIR"/* "$INTEGRATION_DIR"/
    log_info "  Installed to ${INTEGRATION_DIR}"
    ls "$INTEGRATION_DIR" | head -5
    log_info "  ($(ls "$INTEGRATION_DIR" | wc -l | tr -d ' ') files)"
else
    log_error "  Integration source files not found!"
    exit 1
fi

# ── Step 2: Install card resources ───────────────────────────

log_info ""
log_info "Step 2/4: Installing dashboard cards..."

CARDS_DIR="/config/www/smartvanio"
SRC_CARDS="/opt/smartvanio/cards"

mkdir -p "$CARDS_DIR"

if [ -d "$SRC_CARDS" ] && [ "$(ls -A "$SRC_CARDS" 2>/dev/null)" ]; then
    cp -r "$SRC_CARDS"/* "$CARDS_DIR"/
    log_info "  Installed to ${CARDS_DIR}"
    ls -lh "$CARDS_DIR"
else
    log_error "  Card source files not found!"
    exit 1
fi

# ── Step 3: Install dashboard YAML ──────────────────────────

log_info ""
log_info "Step 3/4: Installing dashboard..."

DASHBOARDS_DIR="/config/dashboards"
SRC_DASHBOARDS="/opt/smartvanio/dashboards"

mkdir -p "$DASHBOARDS_DIR"

if [ -f "$SRC_DASHBOARDS/smartvanio.yaml" ]; then
    cp "$SRC_DASHBOARDS/smartvanio.yaml" "$DASHBOARDS_DIR/smartvanio.yaml"
    log_info "  Installed dashboards/smartvanio.yaml"
else
    log_warn "  Dashboard YAML not found — skipping"
fi

# Ensure configuration.yaml has the dashboard + resource entries
CONFIG_FILE="/config/configuration.yaml"
if [ -f "$CONFIG_FILE" ]; then
    # Check if our dashboard is already registered
    if grep -q "smartvan-io:" "$CONFIG_FILE" 2>/dev/null; then
        log_info "  Dashboard already registered in configuration.yaml"
    else
        log_info "  Adding SmartVan.io dashboard to configuration.yaml..."
        cat >> "$CONFIG_FILE" <<'YAMLEOF'

# SmartVan.io — added by setup add-on
lovelace:
  mode: yaml
  resources:
    - url: /local/smartvanio/kiosk-mode.js
      type: module
    - url: /local/smartvanio/smartvanio-main-card.js
      type: module
  dashboards:
    smartvan-io:
      mode: yaml
      title: SmartVan.io
      icon: mdi:van-utility
      show_in_sidebar: true
      filename: dashboards/smartvanio.yaml
YAMLEOF
        log_info "  Added lovelace config to configuration.yaml"
    fi
else
    log_warn "  configuration.yaml not found at ${CONFIG_FILE}"
fi

# ── Step 4: Configure integrations (Supervisor mode only) ────

log_info ""
log_info "Step 4/4: Configuring integrations..."

if [ "$MODE" = "supervisor" ]; then
    # ── Check MQTT broker ────────────────────────────────────
    MOSQUITTO_SLUG="core_mosquitto"
    if bashio::addons.installed "${MOSQUITTO_SLUG}" 2>/dev/null; then
        log_info "  Mosquitto add-on is installed"
        ADDON_STATE=$(bashio::addons.info "${MOSQUITTO_SLUG}" "state" 2>/dev/null || echo "unknown")
        if [ "${ADDON_STATE}" != "started" ]; then
            log_warn "  Starting Mosquitto..."
            bashio::addon.start "${MOSQUITTO_SLUG}" 2>/dev/null || true
            sleep 5
        fi
        MQTT_HOST="core-mosquitto"
    else
        log_warn "  Mosquitto add-on not installed"
        log_warn "  Install it from the Add-on Store, then re-run this add-on"
        MQTT_HOST="localhost"
    fi

    if wait_for_ha; then
        # Register Lovelace resources via API (in case storage mode is used)
        EXISTING=$(ha_api GET "/config/lovelace/resources" 2>/dev/null || echo "[]")

        KIOSK_EXISTS=$(echo "$EXISTING" | jq -r '[.[] | select(.url | contains("kiosk-mode"))] | length' 2>/dev/null || echo "0")
        if [ "$KIOSK_EXISTS" = "0" ]; then
            ha_api POST "/config/lovelace/resources" \
                '{"res_type":"module","url":"/local/smartvanio/kiosk-mode.js"}' >/dev/null 2>&1 || true
            log_info "  Registered kiosk-mode.js resource"
        fi

        MAIN_CARD_EXISTS=$(echo "$EXISTING" | jq -r '[.[] | select(.url | contains("smartvanio-main-card"))] | length' 2>/dev/null || echo "0")
        if [ "$MAIN_CARD_EXISTS" = "0" ]; then
            ha_api POST "/config/lovelace/resources" \
                '{"res_type":"module","url":"/local/smartvanio/smartvanio-main-card.js"}' >/dev/null 2>&1 || true
            log_info "  Registered smartvanio-main-card.js resource"
        fi

        # Configure MQTT
        ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
        MQTT_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "mqtt")] | length' 2>/dev/null || echo "0")

        if [ "$MQTT_EXISTS" = "0" ]; then
            FLOW=$(ha_api POST "/config/config_entries/flow" \
                '{"handler":"mqtt","show_advanced_options":false}' 2>/dev/null || echo "")
            FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)
            if [ -n "$FLOW_ID" ]; then
                RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                    "{\"broker\":\"${MQTT_HOST}\",\"port\":1883,\"username\":\"${MQTT_USER}\",\"password\":\"${MQTT_PASSWORD}\"}" 2>/dev/null)
                RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)
                if [ "$RESULT_TYPE" = "create_entry" ]; then
                    log_info "  MQTT configured (broker: ${MQTT_HOST})"
                else
                    log_warn "  MQTT config flow: ${RESULT_TYPE} — configure manually"
                fi
            fi
        else
            log_info "  MQTT already configured"
        fi

        # Configure SmartVan.io integration
        ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
        SV_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "smartvanio")] | length' 2>/dev/null || echo "0")

        if [ "$SV_EXISTS" = "0" ]; then
            # May need HA restart to detect the new custom component
            FLOW=$(ha_api POST "/config/config_entries/flow" \
                '{"handler":"smartvanio","show_advanced_options":false}' 2>/dev/null || echo "")
            FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)

            if [ -n "$FLOW_ID" ]; then
                RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                    '{"mqtt_prefix":"smartvanio"}' 2>/dev/null)
                RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)
                if [ "$RESULT_TYPE" = "create_entry" ]; then
                    log_info "  SmartVan.io integration configured"
                else
                    log_warn "  SmartVan.io config: ${RESULT_TYPE}"
                    log_warn "  Restart HA, then re-run this add-on"
                fi
            else
                log_info "  Requesting HA restart to load the integration..."
                ha_api POST "/services/homeassistant/restart" '{}' >/dev/null 2>&1 || true
                sleep 30
                if wait_for_ha; then
                    FLOW=$(ha_api POST "/config/config_entries/flow" \
                        '{"handler":"smartvanio","show_advanced_options":false}' 2>/dev/null || echo "")
                    FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)
                    if [ -n "$FLOW_ID" ]; then
                        ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                            '{"mqtt_prefix":"smartvanio"}' >/dev/null 2>&1 || true
                        log_info "  SmartVan.io integration configured after restart"
                    fi
                fi
            fi
        else
            log_info "  SmartVan.io already configured"
        fi
    fi
else
    log_info "  Skipping API configuration (standalone mode)"
    log_info "  Restart HA to load the integration, then configure via UI:"
    log_info "    1. Settings -> Devices & Services -> Add Integration -> MQTT"
    log_info "    2. Settings -> Devices & Services -> Add Integration -> SmartVan.io"
fi

# ── Summary ──────────────────────────────────────────────────

log_info ""
log_info "============================================="
log_info "  SmartVan.io setup complete!"
log_info ""
log_info "  Installed:"
log_info "    Integration: /config/custom_components/smartvanio/"
log_info "    Cards:       /config/www/smartvanio/"
log_info "    Dashboard:   /config/dashboards/smartvanio.yaml"
log_info ""
if [ "$MODE" = "standalone" ]; then
    log_info "  Restart HA to pick up the changes."
fi
log_info "============================================="

# In Supervisor mode, keep alive for log viewing
if [ "$MODE" = "supervisor" ]; then
    while true; do sleep 3600; done
fi
