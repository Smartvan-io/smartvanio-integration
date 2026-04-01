#!/bin/bash
# Stage integration + card files into this directory for Supervisor to build.
# Run this before starting the devcontainer.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

echo "Staging files for local Supervisor build..."

# Integration
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

# Cards
rm -rf "$SCRIPT_DIR/cards"
mkdir -p "$SCRIPT_DIR/cards"
CARDS_SRC="$REPO_ROOT/smartvan.io-cards/main-card"
echo "Building main card..."
(cd "$CARDS_SRC" && npm run build 2>&1 | tail -2)
cp "$CARDS_SRC/index.js" "$SCRIPT_DIR/cards/smartvanio-main-card.js"
[ -f "$CARDS_SRC/kiosk-mode.js" ] && cp "$CARDS_SRC/kiosk-mode.js" "$SCRIPT_DIR/cards/"

echo "Staged: $(ls "$SCRIPT_DIR/integration/" | wc -l | tr -d ' ') integration files, $(ls "$SCRIPT_DIR/cards/" | wc -l | tr -d ' ') card files"
echo "Done."
