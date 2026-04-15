#!/usr/bin/with-contenv bashio
# SmartVan.io Setup App
# Provisions the full SmartVan.io stack into Home Assistant:
#   - Mosquitto MQTT broker
#   - SmartVan.io integration (from GitHub)
#   - Dashboard cards (from GitHub)
#   - Lovelace dashboard + kiosk mode
#   - MQTT + SmartVan.io config entries
#
# The bashio shebang loads s6 env vars (SUPERVISOR_TOKEN) and the
# bashio library automatically. No need to source bashio manually.

# ── Detect environment ───────────────────────────────────────

if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    MODE="supervisor"
else
    MODE="standalone"
fi

bashio::log.info "============================================="
bashio::log.info "  SmartVan.io Setup (mode: ${MODE})"
bashio::log.info "============================================="

MQTT_USER=$(bashio::config 'mqtt_user' 2>/dev/null || echo "")
MQTT_PASSWORD=$(bashio::config 'mqtt_password' 2>/dev/null || echo "")
MQTT_USER="${MQTT_USER:-smartvanio}"
MQTT_PASSWORD="${MQTT_PASSWORD:-smartvanio123}"

INTEGRATION_REPO="https://github.com/Smartvan-io/smartvanio-integration.git"
INTEGRATION_BRANCH="beta"
CARD_BASE_URL="https://raw.githubusercontent.com/Smartvan-io/smartvanio-main-card/refs/heads/beta"

# ── Helpers ──────────────────────────────────────────────────

ha_api() {
    local method="$1"
    local endpoint="$2"
    local data="${3:-}"

    if [ "$MODE" = "supervisor" ]; then
        local url="http://supervisor/core/api${endpoint}"
        local token="${SUPERVISOR_TOKEN:-}"
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
    bashio::log.info "Waiting for Home Assistant API..."
    for i in $(seq 1 60); do
        RESULT=$(ha_api GET "/" 2>/dev/null || echo "")
        if echo "$RESULT" | grep -q "API running"; then
            bashio::log.info "  Home Assistant API is ready"
            return 0
        fi
        sleep 2
    done
    bashio::log.warning "  Home Assistant API not available after 120s"
    return 1
}

# ── Step 1: Provision Mosquitto ──────────────────────────────

bashio::log.info ""
bashio::log.info "Step 1/5: Provisioning MQTT broker..."

if [ "$MODE" = "supervisor" ]; then
    MOSQUITTO_SLUG="core_mosquitto"

    if bashio::addons.installed "${MOSQUITTO_SLUG}" 2>/dev/null; then
        bashio::log.info "  Mosquitto app already installed"
    else
        bashio::log.info "  Installing Mosquitto app..."
        bashio::addon.install "${MOSQUITTO_SLUG}" 2>/dev/null || {
            bashio::log.error "  Failed to install Mosquitto — install it manually from the App Store"
        }
        sleep 10
    fi

    # Ensure it's running
    ADDON_STATE=$(bashio::addons.info "${MOSQUITTO_SLUG}" "state" 2>/dev/null || echo "unknown")
    if [ "${ADDON_STATE}" != "started" ]; then
        bashio::log.info "  Starting Mosquitto..."
        bashio::addon.start "${MOSQUITTO_SLUG}" 2>/dev/null || true
        sleep 5
    fi
    bashio::log.info "  Mosquitto is running"
    MQTT_HOST="core-mosquitto"
else
    bashio::log.info "  Skipping Mosquitto install (standalone mode)"
    MQTT_HOST="localhost"
fi

# ── Step 2: Install integration from GitHub ──────────────────

bashio::log.info ""
bashio::log.info "Step 2/5: Installing SmartVan.io integration..."

INTEGRATION_DIR="/config/custom_components/smartvanio"
TMP_DIR=$(mktemp -d)

bashio::log.info "  Cloning ${INTEGRATION_BRANCH} branch..."
if git clone --depth 1 --branch "$INTEGRATION_BRANCH" "$INTEGRATION_REPO" "$TMP_DIR" 2>/dev/null; then
    rm -rf "$INTEGRATION_DIR"
    mkdir -p "$INTEGRATION_DIR"
    cp -r "$TMP_DIR/custom_components/smartvanio/"* "$INTEGRATION_DIR"/
    rm -rf "$TMP_DIR"
    bashio::log.info "  Installed to ${INTEGRATION_DIR}"
    bashio::log.info "  ($(ls "$INTEGRATION_DIR" | wc -l | tr -d ' ') files)"
else
    rm -rf "$TMP_DIR"
    bashio::log.error "  Failed to clone integration from GitHub"
    bashio::log.error "  Check network connectivity and try again"
    exit 1
fi

# ── Step 3: Install dashboard cards from GitHub ──────────────

bashio::log.info ""
bashio::log.info "Step 3/5: Installing dashboard cards..."

CARDS_DIR="/config/www/smartvanio"
mkdir -p "$CARDS_DIR"

# Main card
bashio::log.info "  Downloading smartvanio-main-card.js..."
if curl -sL "${CARD_BASE_URL}/index.js" -o "${CARDS_DIR}/smartvanio-main-card.js"; then
    CARD_SIZE=$(wc -c < "${CARDS_DIR}/smartvanio-main-card.js" | tr -d ' ')
    if [ "$CARD_SIZE" -gt 1000 ]; then
        bashio::log.info "  smartvanio-main-card.js (${CARD_SIZE} bytes)"
    else
        bashio::log.error "  smartvanio-main-card.js download looks too small (${CARD_SIZE} bytes)"
    fi
else
    bashio::log.error "  Failed to download main card"
fi

# Kiosk mode
bashio::log.info "  Downloading kiosk-mode.js..."
if curl -sL "${CARD_BASE_URL}/kiosk-mode.js" -o "${CARDS_DIR}/kiosk-mode.js"; then
    bashio::log.info "  kiosk-mode.js downloaded"
else
    bashio::log.warning "  Failed to download kiosk-mode.js — kiosk mode will not be available"
fi

# ── Step 4: Install dashboard + configure lovelace ───────────

bashio::log.info ""
bashio::log.info "Step 4/5: Installing dashboard..."

# Copy dashboard YAML
DASHBOARDS_DIR="/config/dashboards"
SRC_DASHBOARDS="/opt/smartvanio/dashboards"

mkdir -p "$DASHBOARDS_DIR"

if [ -f "$SRC_DASHBOARDS/smartvanio.yaml" ]; then
    cp "$SRC_DASHBOARDS/smartvanio.yaml" "$DASHBOARDS_DIR/smartvanio.yaml"
    bashio::log.info "  Installed dashboards/smartvanio.yaml"
else
    bashio::log.warning "  Dashboard YAML not found — skipping"
fi

# Safely merge lovelace config into configuration.yaml
CONFIG_FILE="/config/configuration.yaml"
if [ -f "$CONFIG_FILE" ]; then
    if python3 /opt/smartvanio/configure_yaml.py "$CONFIG_FILE" 2>/dev/null; then
        bashio::log.info "  Lovelace config merged into configuration.yaml"
    else
        bashio::log.warning "  Python YAML merge failed — trying fallback append"
        # Fallback: append only if our dashboard isn't already registered
        if ! grep -q "smartvan-io:" "$CONFIG_FILE" 2>/dev/null; then
            cat >> "$CONFIG_FILE" <<'YAMLEOF'

# SmartVan.io — added by setup app
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
            bashio::log.info "  Added lovelace config (fallback append)"
        else
            bashio::log.info "  Dashboard already registered in configuration.yaml"
        fi
    fi
else
    bashio::log.warning "  configuration.yaml not found at ${CONFIG_FILE}"
fi

# ── Step 5: Configure integrations (Supervisor mode only) ────

bashio::log.info ""
bashio::log.info "Step 5/5: Configuring integrations..."

if [ "$MODE" = "supervisor" ]; then
    if wait_for_ha; then
        # Register Lovelace resources via API (for storage mode users)
        EXISTING=$(ha_api GET "/config/lovelace/resources" 2>/dev/null || echo "[]")

        KIOSK_EXISTS=$(echo "$EXISTING" | jq -r '[.[] | select(.url | contains("kiosk-mode"))] | length' 2>/dev/null || echo "0")
        if [ "$KIOSK_EXISTS" = "0" ]; then
            ha_api POST "/config/lovelace/resources" \
                '{"res_type":"module","url":"/local/smartvanio/kiosk-mode.js"}' >/dev/null 2>&1 || true
            bashio::log.info "  Registered kiosk-mode.js resource"
        fi

        MAIN_CARD_EXISTS=$(echo "$EXISTING" | jq -r '[.[] | select(.url | contains("smartvanio-main-card"))] | length' 2>/dev/null || echo "0")
        if [ "$MAIN_CARD_EXISTS" = "0" ]; then
            ha_api POST "/config/lovelace/resources" \
                '{"res_type":"module","url":"/local/smartvanio/smartvanio-main-card.js"}' >/dev/null 2>&1 || true
            bashio::log.info "  Registered smartvanio-main-card.js resource"
        fi

        # Configure MQTT integration
        ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
        MQTT_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "mqtt")] | length' 2>/dev/null || echo "0")

        if [ "$MQTT_EXISTS" = "0" ]; then
            FLOW=$(ha_api POST "/config/config_entries/flow" \
                '{"handler":"mqtt","show_advanced_options":false}' 2>/dev/null || echo "")
            FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)
            FLOW_TYPE=$(echo "$FLOW" | jq -r '.type // empty' 2>/dev/null)
            if [ -n "$FLOW_ID" ]; then
                if [ "$FLOW_TYPE" = "menu" ]; then
                    # Mosquitto is installed — select the addon option for auto-config
                    RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                        '{"next_step_id":"addon"}' 2>/dev/null)
                else
                    # No Mosquitto — configure broker manually
                    RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                        "{\"broker\":\"${MQTT_HOST}\",\"port\":1883,\"username\":\"${MQTT_USER}\",\"password\":\"${MQTT_PASSWORD}\"}" 2>/dev/null)
                fi
                RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)
                if [ "$RESULT_TYPE" = "create_entry" ]; then
                    bashio::log.info "  MQTT configured"
                else
                    bashio::log.warning "  MQTT config flow: ${RESULT_TYPE} — configure manually"
                fi
            fi
        else
            bashio::log.info "  MQTT already configured"
        fi

        # Configure SmartVan.io integration
        ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
        SV_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "smartvanio")] | length' 2>/dev/null || echo "0")

        if [ "$SV_EXISTS" = "0" ]; then
            FLOW=$(ha_api POST "/config/config_entries/flow" \
                '{"handler":"smartvanio","show_advanced_options":false}' 2>/dev/null || echo "")
            FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)

            if [ -n "$FLOW_ID" ]; then
                RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                    '{"mqtt_prefix":"smartvanio"}' 2>/dev/null)
                RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)
                if [ "$RESULT_TYPE" = "create_entry" ]; then
                    bashio::log.info "  SmartVan.io integration configured"
                else
                    bashio::log.warning "  SmartVan.io config: ${RESULT_TYPE}"
                    bashio::log.warning "  Restart HA, then re-run this app"
                fi
            else
                # Integration not yet loaded — restart HA and retry
                bashio::log.info "  Requesting HA restart to load the integration..."
                ha_api POST "/services/homeassistant/restart" '{}' >/dev/null 2>&1 || true
                sleep 30
                if wait_for_ha; then
                    FLOW=$(ha_api POST "/config/config_entries/flow" \
                        '{"handler":"smartvanio","show_advanced_options":false}' 2>/dev/null || echo "")
                    FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)
                    if [ -n "$FLOW_ID" ]; then
                        ha_api POST "/config/config_entries/flow/${FLOW_ID}" \
                            '{"mqtt_prefix":"smartvanio"}' >/dev/null 2>&1 || true
                        bashio::log.info "  SmartVan.io integration configured after restart"
                    fi
                fi
            fi
        else
            bashio::log.info "  SmartVan.io already configured"
        fi
    fi
else
    bashio::log.info "  Skipping API configuration (standalone mode)"
    bashio::log.info "  Restart HA to load the integration, then configure via UI:"
    bashio::log.info "    1. Settings -> Devices & Services -> Add Integration -> MQTT"
    bashio::log.info "    2. Settings -> Devices & Services -> Add Integration -> SmartVan.io"
fi

# ── Summary ──────────────────────────────────────────────────

bashio::log.info ""
bashio::log.info "============================================="
bashio::log.info "  SmartVan.io setup complete!"
bashio::log.info ""
bashio::log.info "  Installed:"
bashio::log.info "    Integration: /config/custom_components/smartvanio/"
bashio::log.info "    Cards:       /config/www/smartvanio/"
bashio::log.info "    Dashboard:   /config/dashboards/smartvanio.yaml"
bashio::log.info ""
if [ "$MODE" = "standalone" ]; then
    bashio::log.info "  Restart HA to pick up the changes."
fi
bashio::log.info "============================================="

# Keep alive for log viewing (required for addon containers)
while true; do sleep 3600; done
