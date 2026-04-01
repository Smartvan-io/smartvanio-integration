# SmartVan.io Add-on — Dev Environment

Development environment for testing the SmartVan.io add-on.
Runs HA + Mosquitto with the integration, cards, and dashboard pre-mounted.

## Quick Start

```bash
cd smartvan.io-integration/addon/dev
docker compose up -d
```

Open http://localhost:8125 and complete HA onboarding (create account).

## After Onboarding

1. **Add MQTT integration**: Settings → Devices & Services → Add Integration → MQTT
   - Broker: `smartvanio-addon-dev-mqtt`
   - Port: `1883`
   - Username: `smartvanio`
   - Password: `smartvanio123`

2. **Add SmartVan.io integration**: Settings → Devices & Services → Add Integration → SmartVan.io
   - MQTT prefix: `smartvanio` (default)

3. **Open dashboard**: Click "SmartVan.io" in the sidebar

## Rebuilding the Card

```bash
cd smartvan.io-cards/main-card
npm run build    # or: npm run dev (watch mode)
```

Then hard-refresh the browser (Cmd+Shift+R).

## Ports

| Service        | Port |
|---------------|------|
| Home Assistant | 8125 |
| MQTT           | 1884 |
| MQTT WebSocket | 9002 |

## Teardown

```bash
docker compose down -v   # removes volumes too
```
