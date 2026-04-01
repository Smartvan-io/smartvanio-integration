#!/bin/bash
# Build the SmartVan.io add-on — stages bundled files then builds Docker image.
# Run from the addon/ directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== SmartVan.io Add-on Build ==="
echo ""

# ── Stage integration files ──────────────────────────────────
echo "Staging integration files..."
rm -rf "$SCRIPT_DIR/integration"
mkdir -p "$SCRIPT_DIR/integration/translations"

INTEGRATION_SRC="$REPO_ROOT/smartvan.io-integration/custom_components/smartvanio"

cp "$INTEGRATION_SRC"/__init__.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/manifest.json "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/const.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/config_flow.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/light.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/sensor.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/switch.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/binary_sensor.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/button.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/number.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/select.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/scene.py "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/strings.json "$SCRIPT_DIR/integration/"
cp "$INTEGRATION_SRC"/translations/en.json "$SCRIPT_DIR/integration/translations/"

echo "  Staged $(ls "$SCRIPT_DIR/integration/" | wc -l | tr -d ' ') files"

# ── Stage card files ─────────────────────────────────────────
echo "Staging card files..."
rm -rf "$SCRIPT_DIR/cards"
mkdir -p "$SCRIPT_DIR/cards"

CARDS_SRC="$REPO_ROOT/smartvan.io-cards/main-card"

# Build the card first
echo "  Building main card..."
(cd "$CARDS_SRC" && npm run build 2>&1 | tail -2)

cp "$CARDS_SRC/index.js" "$SCRIPT_DIR/cards/smartvanio-main-card.js"

# Kiosk mode (if it exists as a separate file)
if [ -f "$CARDS_SRC/kiosk-mode.js" ]; then
    cp "$CARDS_SRC/kiosk-mode.js" "$SCRIPT_DIR/cards/kiosk-mode.js"
fi

echo "  Staged $(ls "$SCRIPT_DIR/cards/" | wc -l | tr -d ' ') card files"

# ── Build Docker image ───────────────────────────────────────
echo ""
echo "Building Docker image..."

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  BASE="ghcr.io/home-assistant/amd64-base:3.20" ;;
    aarch64) BASE="ghcr.io/home-assistant/aarch64-base:3.20" ;;
    armv7l)  BASE="ghcr.io/home-assistant/armv7-base:3.20" ;;
    *)       BASE="ghcr.io/home-assistant/amd64-base:3.20" ;;
esac

docker build \
    --build-arg BUILD_FROM="$BASE" \
    -t smartvanio-addon:local \
    "$SCRIPT_DIR"

echo ""
echo "=== Build complete: smartvanio-addon:local ==="
