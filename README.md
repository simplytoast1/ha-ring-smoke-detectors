# Ring Smoke Detectors for Home Assistant

A [HACS](https://hacs.xyz) custom integration for **Kidde/Ring smart smoke and CO detectors** — the WiFi-only, hubless models that are not supported by the standard [Ring integration](https://www.home-assistant.io/integrations/ring/).

## Why This Integration Exists

Kidde/Ring smart smoke and CO detectors connect via WiFi and are managed through the Ring app. However, these devices don't work with the standard Ring integration because:

1. **Real-time alarm state** (smoke detected, CO detected) is only available via a WebSocket connection
2. The upstream library only creates WebSocket connections when a Ring Alarm hub or Beams bridge is present — but these Kidde detectors work without any hub
3. These devices may not appear in the Ring REST API at all — they are only reliably discoverable via WebSocket

This integration establishes its own WebSocket connections that bypass the hub requirement. This approach was [discovered by @tsightler](https://github.com/dgreif/ring/issues/1674#issuecomment-4094895140) and [validated by @jbettcher](https://github.com/dgreif/ring/compare/main...jbettcher:ring:kidde_ring_support).

## Supported Devices

| Device | Model |
|--------|-------|
| Kidde/Ring Smart Smoke Alarm (wired) | Smoke only |
| Kidde/Ring Smart Smoke + CO Alarm (wired) | Smoke + CO |
| Kidde/Ring Smart Smoke + CO Alarm (battery) | Smoke + CO |

## Entities

Each detector exposes the following Home Assistant entities:

### Binary Sensors
- **Smoke** — `on` when smoke is detected (all models)
- **Carbon Monoxide** — `on` when CO is detected (CO models only)

### Sensors
- **Battery** — battery level percentage (all models)
- **CO Level** — carbon monoxide level in PPM (CO models only)

## Prerequisites

- [Home Assistant](https://www.home-assistant.io/) 2024.1 or later
- [HACS](https://hacs.xyz) installed
- A Ring account with Kidde/Ring smoke detectors set up in the Ring app

## Installation via HACS

1. Open HACS in your Home Assistant instance
2. Click the three dots in the top right corner and select **Custom repositories**
3. Paste the URL of this GitHub repository and select **Integration** as the category
4. Click **Add**
5. Find **Ring Smoke Detectors** in the HACS store and click **Install**
6. Restart Home Assistant

## Manual Installation

1. Download or clone this repository
2. Copy the `custom_components/ring_smoke_detectors` folder into your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## Setup

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Ring Smoke Detectors**
3. Enter your **Ring email** and **password**
4. If prompted, enter your **2FA verification code**
5. Your devices will be discovered automatically

Your Ring credentials are sent directly to Ring's servers and are **not stored**. Only the resulting refresh token is saved, and the integration automatically handles token rotation.

## How It Works

1. **Authentication** — OAuth token management with automatic token rotation and persistence
2. **Location Discovery** — Fetches all Ring locations, then probes each one via WebSocket
3. **WebSocket Connection** — Requests a ticket from Ring's `clap/tickets` endpoint and establishes a direct WebSocket connection — even without a Ring hub
4. **Device Discovery** — Sends `DeviceInfoDocGetList` requests over the WebSocket to discover devices and their current state
5. **Real-time Updates** — Subscribes to `DataUpdate` messages for live alarm state changes (smoke, CO, battery, etc.)
6. **Auto-reconnect** — Reconnects with exponential backoff (5s → 60s) on connection loss

## Troubleshooting

### Devices Not Showing Up

- Ensure your Kidde/Ring detectors are set up and online in the Ring app
- Check the Home Assistant logs for discovery messages
- Enable debug logging by adding to `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.ring_smoke_detectors: debug
  ```

### Token Issues

The integration automatically rotates and persists refresh tokens. If authentication fails, remove and re-add the integration.

### WebSocket Connection Issues

The integration automatically reconnects with exponential backoff (5s, 10s, 20s, up to 60s). Check Home Assistant logs for connection status messages.

## Credits

This integration is based on the discovery work from [dgreif/ring#1674](https://github.com/dgreif/ring/issues/1674):

- **[@tsightler](https://github.com/tsightler)** — [Discovered](https://github.com/dgreif/ring/issues/1674#issuecomment-4094895140) that Kidde smoke detectors can be accessed via WebSocket even without a hub
- **[@jbettcher](https://github.com/jbettcher)** — [Built the proof-of-concept](https://github.com/dgreif/ring/compare/main...jbettcher:ring:kidde_ring_support) that validated the WebSocket approach
- **[@dgreif](https://github.com/dgreif)** — Creator of [ring-client-api](https://github.com/dgreif/ring)

## License

MIT
