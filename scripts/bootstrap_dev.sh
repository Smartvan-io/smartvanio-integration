#!/bin/bash
# SmartVan.io Dev Environment Bootstrap
#
# Waits for Home Assistant to start, then automatically configures:
#   1. MQTT integration (pointing to the Mosquitto container)
#   2. SmartVan.io custom integration
#
# Prerequisites:
#   - HA must be running (docker compose up -d)
#   - You must have completed HA onboarding (created a user account)
#   - You need a long-lived access token from HA
#
# Usage:
#   1. Get a token: HA → Profile → Long-Lived Access Tokens → Create
#   2. Run: HA_TOKEN="your_token_here" bash scripts/bootstrap_dev.sh
#   OR:
#   3. Run: bash scripts/bootstrap_dev.sh
#      (it will prompt you for the token)

set -e

HA_URL="${HA_URL:-http://localhost:8124}"
MQTT_HOST="${MQTT_HOST:-mosquitto}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_USER="${MQTT_USER:-smartvanio}"
MQTT_PASSWORD="${MQTT_PASSWORD:-smartvanio123}"
DEVICE_PREFIX="${DEVICE_PREFIX:-smartvanio}"

# ─── Get token ────────────────────────────────────────────────
if [ -z "$HA_TOKEN" ]; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  You need a Home Assistant long-lived access token.     ║"
    echo "║                                                         ║"
    echo "║  To create one:                                         ║"
    echo "║  1. Open ${HA_URL}                          ║"
    echo "║  2. Click your profile (bottom-left)                    ║"
    echo "║  3. Scroll to 'Long-Lived Access Tokens'                ║"
    echo "║  4. Click 'Create Token' → name it 'smartvanio-dev'    ║"
    echo "║  5. Copy the token and paste it below                   ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""
    read -r -p "Paste your HA token: " HA_TOKEN
    echo ""
fi

if [ -z "$HA_TOKEN" ]; then
    echo "Error: No token provided. Exiting."
    exit 1
fi

# ─── Helpers ──────────────────────────────────────────────────
ha_api() {
    local method="$1"
    local endpoint="$2"
    local data="$3"

    if [ -n "$data" ]; then
        curl -s -X "$method" \
            -H "Authorization: Bearer ${HA_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "${HA_URL}/api${endpoint}"
    else
        curl -s -X "$method" \
            -H "Authorization: Bearer ${HA_TOKEN}" \
            -H "Content-Type: application/json" \
            "${HA_URL}/api${endpoint}"
    fi
}

echo "═══════════════════════════════════════════"
echo "  SmartVan.io Dev Bootstrap"
echo "═══════════════════════════════════════════"
echo ""

# ─── Wait for HA ──────────────────────────────────────────────
echo "→ Checking Home Assistant is available..."
for i in $(seq 1 30); do
    RESULT=$(ha_api GET "/" 2>/dev/null || echo "")
    if echo "$RESULT" | grep -q "API running"; then
        echo "  ✓ Home Assistant API is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Home Assistant is not responding at ${HA_URL}"
        echo "    Make sure 'docker compose up -d' is running"
        echo "    and you've completed onboarding."
        exit 1
    fi
    echo "  Waiting... (${i}/30)"
    sleep 3
done

# ─── Step 1: Configure MQTT integration ──────────────────────
echo ""
echo "→ Step 1/2: Configuring MQTT integration..."

# Check if MQTT is already configured
ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
MQTT_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "mqtt")] | length' 2>/dev/null || echo "0")

if [ "$MQTT_EXISTS" -gt "0" ]; then
    echo "  ✓ MQTT integration already configured — skipping"
else
    # Start the config flow for MQTT
    FLOW=$(ha_api POST "/config/config_entries/flow" \
        '{"handler": "mqtt", "show_advanced_options": false}' 2>/dev/null)

    FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)

    if [ -n "$FLOW_ID" ]; then
        MQTT_CONFIG=$(cat <<EOF
{
    "broker": "${MQTT_HOST}",
    "port": ${MQTT_PORT},
    "username": "${MQTT_USER}",
    "password": "${MQTT_PASSWORD}"
}
EOF
)
        RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" "$MQTT_CONFIG" 2>/dev/null)
        RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)

        if [ "$RESULT_TYPE" = "create_entry" ]; then
            echo "  ✓ MQTT integration configured (broker: ${MQTT_HOST}:${MQTT_PORT})"
        else
            echo "  ⚠ MQTT config flow returned: ${RESULT_TYPE}"
            echo "    Response: $(echo "$RESULT" | jq -c '.' 2>/dev/null)"
            echo "    You may need to configure MQTT manually."
        fi
    else
        echo "  ⚠ Could not start MQTT config flow."
        echo "    Flow response: $(echo "$FLOW" | jq -c '.' 2>/dev/null)"
    fi
fi

# ─── Step 2: Configure SmartVan.io integration ───────────────
echo ""
echo "→ Step 2/2: Configuring SmartVan.io integration..."

# Re-fetch entries (MQTT may have been added)
ENTRIES=$(ha_api GET "/config/config_entries/entry" 2>/dev/null || echo "[]")
SMARTVANIO_EXISTS=$(echo "$ENTRIES" | jq -r '[.[] | select(.domain == "smartvanio")] | length' 2>/dev/null || echo "0")

if [ "$SMARTVANIO_EXISTS" -gt "0" ]; then
    echo "  ✓ SmartVan.io integration already configured — skipping"
else
    FLOW=$(ha_api POST "/config/config_entries/flow" \
        '{"handler": "smartvanio", "show_advanced_options": false}' 2>/dev/null)

    FLOW_ID=$(echo "$FLOW" | jq -r '.flow_id // empty' 2>/dev/null)

    if [ -n "$FLOW_ID" ]; then
        SMARTVANIO_CONFIG="{\"mqtt_prefix\": \"${DEVICE_PREFIX}\"}"
        RESULT=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" "$SMARTVANIO_CONFIG" 2>/dev/null)
        RESULT_TYPE=$(echo "$RESULT" | jq -r '.type // empty' 2>/dev/null)

        if [ "$RESULT_TYPE" = "create_entry" ]; then
            echo "  ✓ SmartVan.io integration configured (prefix: ${DEVICE_PREFIX})"
        else
            echo "  ⚠ SmartVan.io config flow returned: ${RESULT_TYPE}"
            echo "    Response: $(echo "$RESULT" | jq -c '.' 2>/dev/null)"
            echo ""
            echo "    This usually means HA needs a restart to detect the custom component."
            echo "    Try: docker compose restart homeassistant"
            echo "    Then re-run this script."
        fi
    else
        echo "  ✗ SmartVan.io integration not found by Home Assistant."
        echo "    This means HA hasn't loaded the custom component yet."
        echo ""
        echo "    Fix: Restart HA so it picks up the mounted custom_components:"
        echo "      docker compose restart homeassistant"
        echo "    Then re-run this script."
    fi
fi

# ─── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "  ✓ Bootstrap complete!"
echo ""
echo "  Check for SmartVan.io devices in HA:"
echo "    Settings → Devices & Services → SmartVan.io"
echo ""
echo "  Test MQTT manually:"
echo "    mosquitto_sub -h localhost -p 1884 \\"
echo "      -u smartvanio -P smartvanio123 -t 'smartvanio/#' -v"
echo "═══════════════════════════════════════════"
