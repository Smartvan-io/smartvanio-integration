# SmartVan.io Integration

Home Assistant integration for SmartVan.io campervan modules — MQTT-based, with BLE provisioning and an addon-managed setup.

## How to install (everyone)

**Install the SmartVan.io addon.** That's it. The addon installs the latest integration, sets up Mosquitto, configures the MQTT integration, installs the cards, and migrates any existing v1.x devices.

1. Settings → Add-ons → Add-on Store → ⋮ (top-right) → Repositories
2. Add `https://github.com/Smartvan-io/smartvan`
3. Install **SmartVan.io** → Start

The addon takes care of installing this integration. You should not install it manually.

## Upgrading from v1.x (HACS install)

If you previously installed `smartvanio-integration` via HACS (v1.0.x), it's an ESPHome-API wrapper that no longer loads on Home Assistant's Python 3.14 runtime. Install the addon as above — its provisioning script replaces the broken HACS install with v3, sets up MQTT, and offers to reflash any legacy-firmware devices.

The HACS listing for this repo is intentionally pinned to v1.0.1 to prevent it from auto-updating into a half-installed v3 standalone (v3 needs the addon's MQTT setup to function).

## Documentation

- Upgrade guide: [smartvan.io/blogs/guides](https://smartvan.io/blogs/guides)
- Addon repo: [github.com/Smartvan-io/smartvan](https://github.com/Smartvan-io/smartvan)
