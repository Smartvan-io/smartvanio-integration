# SmartVan.io Integration

Home Assistant integration for SmartVan.io campervan modules — MQTT-based, with BLE provisioning and an addon-managed setup.

## How to install (everyone)

**Install the SmartVan.io addon.** That's it. The addon installs the latest integration, sets up Mosquitto, configures the MQTT integration, installs the cards, and migrates any existing v1.x devices.

1. Settings → Add-ons → Add-on Store → ⋮ (top-right) → Repositories
2. Add `https://github.com/Smartvan-io/smartvan`
3. Install **SmartVan.io** → Start

The addon takes care of installing this integration. You should not install it manually.

## Upgrading from HACS installs

As of v3.0.1, this integration is no longer distributed via HACS. The SmartVan.io add-on is the only supported install path — it manages this integration, MQTT, and the dashboard cards together.

If you previously installed via HACS (any v1.x or v3.0.0 release):

1. Install the SmartVan.io add-on as above. It will replace the HACS-installed integration in `/config/custom_components/smartvanio/` with the addon-managed copy and set up MQTT.
2. Remove the `Smartvan-io/smartvanio-integration` custom repository from HACS afterwards.

The original HACS v1.x release was an ESPHome-API wrapper that no longer loads on Home Assistant's Python 3.14 runtime — the addon's provisioning script handles that migration automatically.

## Documentation

- Upgrade guide: [smartvan.io/blogs/guides](https://smartvan.io/blogs/guides)
- Addon repo: [github.com/Smartvan-io/smartvan](https://github.com/Smartvan-io/smartvan)
