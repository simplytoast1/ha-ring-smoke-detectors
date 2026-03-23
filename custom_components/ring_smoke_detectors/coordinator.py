"""Data coordinator for Ring Smoke Detectors.

Manages WebSocket connections to Ring's servers and coordinates device
state updates across all entities. Uses HA's DataUpdateCoordinator with
push-based updates from the WebSocket (fallback hourly poll).
"""

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, CONF_REFRESH_TOKEN, DEVICE_API_BASE, CONF_LOCATION_IDS
from .ring_api.auth import RingRestClient
from .ring_api.websocket import (
    SmokeDetectorWebSocket,
    is_kidde_device_type,
)

_LOGGER = logging.getLogger(__name__)


class RingSmokeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for Ring smoke detector data.

    Discovers Ring locations, establishes WebSocket connections for each
    location with Kidde assets, and pushes real-time state updates to
    HA entities.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
        )
        self.entry = entry
        self.rest_client: RingRestClient | None = None
        self.connections: list[SmokeDetectorWebSocket] = []
        self.devices: dict[str, dict[str, Any]] = {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Discover or refresh devices.

        On first call: authenticates, discovers locations, connects WebSockets.
        On subsequent calls: returns current device data (WebSocket handles live updates).
        """
        if not self.rest_client:
            refresh_token = self.entry.data[CONF_REFRESH_TOKEN]
            self.rest_client = RingRestClient(
                refresh_token=refresh_token,
                on_token_update=self._handle_token_update,
            )

        if not self.connections:
            await self._discover_devices()

        return self.devices

    async def _discover_devices(self) -> None:
        """Discover Ring locations and connect WebSockets.

        For EVERY location, attempts a WebSocket connection via clap/tickets.
        The ticket response reveals which locations have Kidde smoke detector
        assets. This is the only reliable discovery method -- the REST API
        does not reliably list these devices.
        """
        assert self.rest_client is not None

        data = await self.rest_client.request(f"{DEVICE_API_BASE}locations")
        locations = data.get("user_locations", [])

        _LOGGER.info("Found %d Ring location(s)", len(locations))

        # Filter by configured location IDs if set
        location_ids = self.entry.options.get(CONF_LOCATION_IDS)
        if location_ids:
            locations = [
                loc for loc in locations if loc["location_id"] in location_ids
            ]

        if not locations:
            _LOGGER.warning("No Ring locations found for this account")
            return

        for location in locations:
            try:
                ws = SmokeDetectorWebSocket(
                    location["location_id"],
                    location["name"],
                    self.rest_client,
                    on_device_update=self._handle_device_update,
                    on_devices_discovered=self._handle_devices_discovered,
                )

                devices = await ws.connect()

                if not ws.has_assets:
                    _LOGGER.debug(
                        'Location "%s": no Kidde assets, skipping',
                        location["name"],
                    )
                    await ws.disconnect()
                    continue

                self.connections.append(ws)

                for device in devices:
                    device_type = device.get("deviceType", "")
                    if is_kidde_device_type(device_type):
                        self.devices[device["zid"]] = device

            except Exception as err:
                _LOGGER.error(
                    'Failed to connect to location "%s": %s',
                    location["name"],
                    err,
                )

        if not self.devices:
            _LOGGER.warning(
                "No Kidde/Ring smoke detectors found at any location. "
                "Ensure your devices are set up in the Ring app and online."
            )

    def _handle_device_update(self, data: dict) -> None:
        """Handle real-time device update from WebSocket."""
        zid = data.get("zid")
        if not zid:
            return

        if zid in self.devices:
            _LOGGER.debug("Device update: %s (%s)", data.get("name"), zid)
            self.devices[zid] = data
            self.async_set_updated_data(self.devices)
        elif is_kidde_device_type(data.get("deviceType", "")):
            _LOGGER.info(
                "New device detected: %s (%s)",
                data.get("name"),
                data.get("deviceType"),
            )
            self.devices[zid] = data
            self.async_set_updated_data(self.devices)

    def _handle_devices_discovered(self, devices: list[dict]) -> None:
        """Handle device list from WebSocket reconnect (may include new devices)."""
        changed = False
        for device in devices:
            zid = device.get("zid")
            device_type = device.get("deviceType", "")
            if zid and is_kidde_device_type(device_type):
                if zid not in self.devices:
                    _LOGGER.info(
                        "New device on reconnect: %s (%s)",
                        device.get("name"),
                        device_type,
                    )
                    changed = True
                self.devices[zid] = device
        if changed:
            self.async_set_updated_data(self.devices)

    def _handle_token_update(self, new_token: str) -> None:
        """Persist rotated refresh token to config entry."""
        _LOGGER.info("Ring refresh token updated")
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_REFRESH_TOKEN: new_token},
        )

    async def async_shutdown(self) -> None:
        """Clean up WebSocket connections on HA shutdown."""
        _LOGGER.info("Shutting down Ring Smoke Detectors")
        for conn in self.connections:
            await conn.disconnect()
        self.connections.clear()
        if self.rest_client:
            await self.rest_client.close()
            self.rest_client = None
