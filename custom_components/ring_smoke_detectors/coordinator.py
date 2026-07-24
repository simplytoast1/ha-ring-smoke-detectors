"""Data coordinator for Ring Smoke Detectors.

Manages WebSocket connections to Ring's servers and coordinates device
state updates across all entities. Uses HA's DataUpdateCoordinator with
push-based updates from the WebSocket. A dedicated hourly rediscovery
timer (independent of push updates, which reset the coordinator's own
schedule) re-runs discovery so locations that failed to connect or
gained devices are picked up without a restart.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    CONF_REFRESH_TOKEN,
    DEVICE_API_BASE,
    EVENT_RING_SMOKE_DETECTORS,
    EVENT_TYPE_TEST,
    IMPULSE_ALARM_TESTING,
    signal_test_event,
)
from .ring_api.auth import RingApiError, RingAuthError, RingRestClient
from .ring_api.websocket import (
    SmokeDetectorWebSocket,
    is_kidde_device_type,
)

_LOGGER = logging.getLogger(__name__)

RingSmokeConfigEntry = ConfigEntry["RingSmokeCoordinator"]

REDISCOVERY_INTERVAL = timedelta(hours=1)


class RingSmokeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for Ring smoke detector data.

    Discovers Ring locations, establishes WebSocket connections for each
    location with Kidde assets, and pushes real-time state updates to
    HA entities.
    """

    def __init__(self, hass: HomeAssistant, entry: RingSmokeConfigEntry) -> None:
        # No update_interval: push updates via async_set_updated_data
        # would keep resetting it, so rediscovery gets its own timer.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
        )
        self.entry = entry
        self._session = async_get_clientsession(hass)
        self.rest_client = RingRestClient(
            session=self._session,
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            on_token_update=self._handle_token_update,
        )
        # location_id -> live websocket (only locations with Kidde assets)
        self.connections: dict[str, SmokeDetectorWebSocket] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self._device_locations: dict[str, str] = {}
        self._connection_state: dict[str, bool] = {}
        # zid -> monotonic time of the last fired test event (dedupe)
        self._last_test_event: dict[str, float] = {}
        # Serializes discovery so a disconnect-triggered refresh cannot
        # race the scheduled one and create duplicate websockets.
        self._discovery_lock = asyncio.Lock()
        self._unsub_rediscovery = async_track_time_interval(
            hass, self._scheduled_rediscovery, REDISCOVERY_INTERVAL
        )

    async def _scheduled_rediscovery(self, _now: datetime) -> None:
        """Periodic rediscovery of locations and devices."""
        await self.async_request_refresh()

    def is_device_available(self, zid: str) -> bool:
        """Whether the websocket serving this device is connected."""
        location_id = self._device_locations.get(zid)
        if location_id is None:
            return False
        return self._connection_state.get(location_id, False)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Run discovery for locations without a live connection.

        Auth failures surface as ConfigEntryAuthFailed so HA starts a
        reauth flow. Transient API failures only fail the refresh when
        no location is connected at all; otherwise the live websockets
        keep delivering updates and the poll retries next interval.
        """
        try:
            await self._discover_devices()
        except ConfigEntryAuthFailed:
            raise
        except RingAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Ring rejected the stored credentials: {err}"
            ) from err
        except (RingApiError, TimeoutError, OSError) as err:
            if not any(self._connection_state.values()):
                raise UpdateFailed(
                    f"Error communicating with Ring: {err}"
                ) from err
            _LOGGER.warning(
                "Ring discovery poll failed (existing connections are "
                "still live): %s",
                err,
            )
        return self.devices

    async def _discover_devices(self) -> None:
        """Discover Ring locations and connect WebSockets.

        For every location without a live connection, attempts a
        WebSocket connection via clap/tickets. The ticket response
        reveals which locations have Kidde smoke detector assets. This
        is the only reliable discovery method -- the REST API does not
        reliably list these devices.
        """
        async with self._discovery_lock:
            await self._discover_devices_locked()

    async def _discover_devices_locked(self) -> None:
        try:
            data = await self.rest_client.request(f"{DEVICE_API_BASE}locations")
        except RingAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Ring rejected the stored credentials: {err}"
            ) from err

        locations = data.get("user_locations", [])

        if not locations:
            _LOGGER.warning("No Ring locations found for this account")
            return

        failures = 0
        for location in locations:
            location_id = location["location_id"]
            if location_id in self.connections:
                continue

            ws = SmokeDetectorWebSocket(
                location_id,
                location["name"],
                self.rest_client,
                self._session,
                on_device_update=(
                    lambda data, loc=location_id: self._handle_device_update(
                        loc, data
                    )
                ),
                on_devices_discovered=(
                    lambda devs, loc=location_id: self._handle_devices_discovered(
                        loc, devs
                    )
                ),
                on_connection_change=(
                    lambda up, loc=location_id: self._handle_connection_change(
                        loc, up
                    )
                ),
                on_impulse=self._handle_impulse,
            )

            try:
                devices = await ws.connect()
            except RingAuthError as err:
                await ws.disconnect()
                raise ConfigEntryAuthFailed(
                    f"Ring rejected the stored credentials: {err}"
                ) from err
            except Exception as err:
                _LOGGER.error(
                    'Failed to connect to location "%s": %s',
                    location["name"],
                    err,
                )
                await ws.disconnect()
                failures += 1
                continue

            if not ws.has_assets:
                _LOGGER.debug(
                    'Location "%s": no Kidde assets, skipping',
                    location["name"],
                )
                await ws.disconnect()
                continue

            self.connections[location_id] = ws
            self._connection_state[location_id] = ws.connected

            for device in devices:
                if is_kidde_device_type(device.get("deviceType", "")):
                    self.devices[device["zid"]] = device
                    self._device_locations[device["zid"]] = location_id

        if failures and not self.connections:
            raise UpdateFailed(
                f"Could not connect to any Ring location "
                f"({failures} of {len(locations)} failed)"
            )

        if not self.devices:
            _LOGGER.warning(
                "No Kidde/Ring smoke detectors found at any location. "
                "Ensure your devices are set up in the Ring app and online."
            )

    @staticmethod
    def _merge_device_data(
        existing: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge a partial DataUpdate payload into the stored device state.

        DataUpdate messages carry only the changed fields, so replacing
        the stored dict wholesale would wipe battery, name, and alarm
        state that the update did not mention. The components dict is
        merged one level deep for the same reason.
        """
        merged = {**existing, **update}
        old_components = existing.get("components")
        new_components = update.get("components")
        if isinstance(old_components, dict) and isinstance(new_components, dict):
            components = {**old_components}
            for key, value in new_components.items():
                if isinstance(value, dict) and isinstance(
                    components.get(key), dict
                ):
                    components[key] = {**components[key], **value}
                else:
                    components[key] = value
            merged["components"] = components
        return merged

    def _handle_device_update(self, location_id: str, data: dict) -> None:
        """Handle real-time device update from WebSocket."""
        zid = data.get("zid")
        if not zid:
            return

        if zid in self.devices:
            _LOGGER.debug("Device update: %s (%s)", data.get("name"), zid)
            old = self.devices[zid]
            merged = self._merge_device_data(old, data)
            self.devices[zid] = merged
            self._device_locations[zid] = location_id
            self._check_test_signals(zid, old, merged)
            self.async_set_updated_data(self.devices)
        elif is_kidde_device_type(data.get("deviceType", "")):
            _LOGGER.info(
                "New device detected: %s (%s)",
                data.get("name"),
                data.get("deviceType"),
            )
            self.devices[zid] = data
            self._device_locations[zid] = location_id
            self.async_set_updated_data(self.devices)

    def _handle_devices_discovered(
        self, location_id: str, devices: list[dict]
    ) -> None:
        """Handle device list from WebSocket reconnect (may include new devices)."""
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
                # Discovery responses are full documents, so replace
                self.devices[zid] = device
                self._device_locations[zid] = location_id
        # Always notify: alarm or battery changes that happened while the
        # websocket was down only exist in these documents, and entities
        # were last notified with the pre-disconnect snapshot.
        self.async_set_updated_data(self.devices)

    def _handle_connection_change(self, location_id: str, connected: bool) -> None:
        """Track per-location websocket health for entity availability."""
        self._connection_state[location_id] = connected
        if connected:
            # Marks the coordinator successful again after an outage and
            # notifies entities so they leave the unavailable state.
            self.async_set_updated_data(self.devices)
        else:
            _LOGGER.warning(
                "WebSocket for a Ring location disconnected; entities for "
                "its devices are unavailable until it reconnects"
            )
            # A disconnect can mean the token was revoked. Poll now so an
            # auth failure surfaces as a reauth prompt instead of waiting
            # for the next hourly refresh.
            self.hass.async_create_task(self.async_request_refresh())
            self.async_update_listeners()

    def _handle_impulse(self, zid: str, impulse_types: list[str]) -> None:
        """Handle one-shot impulse events from the websocket.

        The bluejay device catalog declares "alarm.testing" as the
        impulse for a test button press. Prefix-match to tolerate
        variants like "alarm.testing.started".
        """
        if zid not in self.devices:
            _LOGGER.debug(
                "Ignoring impulse(s) %s for untracked device %s",
                impulse_types,
                zid,
            )
            return
        for impulse_type in impulse_types:
            if impulse_type.startswith(IMPULSE_ALARM_TESTING):
                self._fire_test_event(zid, f"impulse {impulse_type}")
                return

    def _check_test_signals(
        self, zid: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        """Detect a test button press from component state changes.

        The pushToTest component is confirmed to idle at
        {"status": "inactive"} on real hardware; the active value has
        never been captured publicly, so any other status counts as a
        test. Unknown alarmStatus values are logged (not alarmed on)
        so a real capture can pin the enums down later.
        """
        old_components = old.get("components") or {}
        new_components = new.get("components") or {}

        old_status = (old_components.get("pushToTest") or {}).get("status")
        new_status = (new_components.get("pushToTest") or {}).get("status")
        if (
            new_status is not None
            and new_status != "inactive"
            and new_status != old_status
        ):
            _LOGGER.info(
                "pushToTest status %r observed for %s", new_status, zid
            )
            self._fire_test_event(zid, f"pushToTest status {new_status!r}")

        # Instrumentation: surface undocumented alarm states without
        # treating them as alarms (is_on only matches "active").
        for key in ("alarm.smoke", "alarm.co"):
            status = (new_components.get(key) or {}).get("alarmStatus")
            if status not in (None, "active", "inactive"):
                _LOGGER.info(
                    "Unrecognized %s alarmStatus %r for %s; please report "
                    "this value on the issue tracker",
                    key,
                    status,
                    zid,
                )

    def _fire_test_event(self, zid: str, source: str) -> None:
        """Fire the test event for a device, deduplicated over 30s.

        A single button press can surface through several signals
        (impulse plus component updates), so collapse them into one
        event.
        """
        now = time.monotonic()
        last = self._last_test_event.get(zid)
        if last is not None and now - last < 30:
            return
        self._last_test_event[zid] = now

        device = self.devices.get(zid, {})
        _LOGGER.info(
            "Test button press detected on %s (%s) via %s",
            device.get("name", "unknown"),
            zid,
            source,
        )

        # Notify the event entity for this device
        async_dispatcher_send(
            self.hass, signal_test_event(self.entry.entry_id, zid)
        )

        # Fire a bus event for automations and debugging
        ha_device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, zid)}
        )
        self.hass.bus.async_fire(
            EVENT_RING_SMOKE_DETECTORS,
            {
                "type": EVENT_TYPE_TEST,
                "zid": zid,
                "device_id": ha_device.id if ha_device else None,
                "device_name": device.get("name"),
                "source": source,
            },
        )

    def _handle_token_update(self, new_token: str) -> None:
        """Persist rotated refresh token to config entry."""
        _LOGGER.info("Ring refresh token updated")
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_REFRESH_TOKEN: new_token},
        )

    async def async_shutdown(self) -> None:
        """Clean up WebSocket connections on unload or HA shutdown."""
        _LOGGER.info("Shutting down Ring Smoke Detectors")
        if self._unsub_rediscovery:
            self._unsub_rediscovery()
            self._unsub_rediscovery = None
        await super().async_shutdown()
        for conn in self.connections.values():
            try:
                await conn.disconnect()
            except Exception:
                # One failed disconnect must not strand the others
                _LOGGER.exception(
                    'Error disconnecting websocket for location "%s"',
                    conn.location_name,
                )
        self.connections.clear()
        self._connection_state.clear()
