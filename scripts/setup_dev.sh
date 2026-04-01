#!/bin/bash
# SmartVan.io Development Environment Setup
# Run this once before 'docker compose up -d'

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "╔══════════════════════════════════════════════╗"
echo "║   SmartVan.io Development Environment Setup  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ─── Generate Mosquitto password file ───────────────────────────
echo "→ Generating MQTT password file..."
PASSWD_FILE="$PROJECT_DIR/mosquitto/passwords.txt"

rm -f "$PASSWD_FILE"

docker run --rm --user root -v "$PROJECT_DIR/mosquitto:/mosquitto/config" \
  eclipse-mosquitto:2 \
  mosquitto_passwd -b -c /mosquitto/config/passwords.txt smartvanio smartvanio123

docker run --rm --user root -v "$PROJECT_DIR/mosquitto:/mosquitto/config" \
  eclipse-mosquitto:2 \
  mosquitto_passwd -b /mosquitto/config/passwords.txt homeassistant ha_mqtt_pass

sudo chown "$(id -u):$(id -g)" "$PASSWD_FILE" 2>/dev/null || true

echo "  ✓ MQTT users created: smartvanio, homeassistant"

# ─── Build and start ────────────────────────────────────────────
echo ""
echo "→ Building containers..."
cd "$PROJECT_DIR"
docker compose build

echo ""
echo "═══════════════════════════════════════════════"
echo "  Setup complete! Next steps:"
echo ""
echo "  1. Start the environment:"
echo "     docker compose up -d"
echo ""
echo "  2. Open Home Assistant:"
echo "     http://localhost:8124"
echo ""
echo "  3. Complete HA onboarding (create user account)"
echo ""
echo "  4. Run the bootstrap script:"
echo "     bash scripts/bootstrap_dev.sh"
echo ""
echo "  MQTT credentials for testing:"
echo "     User: smartvanio  |  Password: smartvanio123"
echo "═══════════════════════════════════════════════"
