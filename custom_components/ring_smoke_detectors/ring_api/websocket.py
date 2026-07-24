"""WebSocket connection for Kidde/Ring smoke detectors.

Port of the TypeScript SmokeDetectorWebSocket. This is the core of the
integration -- the key innovation that makes hubless Kidde smoke detectors
work with Home Assistant.

Background (from https://github.com/dgreif/ring/issues/1674):
The existing ring-client-api only creates WebSocket connections when a
location has a Ring hub. But @tsightler discovered that the clap/tickets
endpoint returns sensor_bluejay_* assets even for hubless locations, and
the WebSocket works perfectly with them.

Protocol (same as ring-client-api):
1. GET clap/tickets -- returns assets, host, and auth ticket
2. Filter assets for sensor_bluejay_* kinds
3. Connect to wss://{host}/ws?authcode={ticket}&ack=false
4. Send DeviceInfoDocGetList for each asset UUID
5. Listen for responses and DataUpdate channel messages

Reliability model:
- The websocket is opened with a heartbeat so half-open TCP connections
  are detected and closed by aiohttp, which ends the message loop.
- When the message loop ends (or Ring asks us to reconnect), a single
  reconnect task retries with capped exponential backoff until the
  connection is back or disconnect() is called. The task reference is
  held so it cannot be garbage collected and can be cancelled cleanly.
- Connection state changes are reported through on_connection_change so
  the coordinator can reflect availability on entities.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from ..const import (
    APP_API_BASE,
    KIDDE_DEVICE_TYPE_SMOKE_ONLY,
    KIDDE_KIND_PREFIX,
    KIDDE_KIND_SMOKE_ONLY,
)
from .auth import RingRestClient

_LOGGER = logging.getLogger(__name__)

MAX_RECONNECT_DELAY = 60
INITIAL_RECONNECT_DELAY = 5
WS_HEARTBEAT = 30
DISCOVERY_TIMEOUT = 15


def is_kidde_asset(asset: dict) -> bool:
    """Check if a WebSocket ticket asset is a Kidde smoke detector."""
    return asset.get("kind", "").startswith(KIDDE_KIND_PREFIX)


def is_kidde_device_type(device_type: str) -> bool:
    """Check if a WebSocket deviceType is a Kidde smoke detector."""
    return KIDDE_KIND_PREFIX in device_type


def is_smoke_only(device_type: str) -> bool:
    """Check if a device is smoke-only (no CO sensor)."""
    return device_type in (KIDDE_KIND_SMOKE_ONLY, KIDDE_DEVICE_TYPE_SMOKE_ONLY)


def extract_impulses(data: dict) -> list[str]:
    """Extract impulse event types from a raw DataUpdate body entry.

    Impulses (one-shot events like the test button) arrive as
    { impulse: { v1: [ { impulseType: "alarm.testing" }, ... ] } }
    alongside the general/device sections. flatten_device_data drops
    this section, so it must be read from the raw body.
    """
    impulse = data.get("impulse")
    if not isinstance(impulse, dict):
        return []
    v1 = impulse.get("v1")
    if not isinstance(v1, list):
        return []
    return [
        i["impulseType"]
        for i in v1
        if isinstance(i, dict) and isinstance(i.get("impulseType"), str)
    ]


def flatten_device_data(data: dict) -> dict:
    """Flatten nested WebSocket device data into a single dict.

    WebSocket responses contain device data split across two objects:
      { general: { v2: { zid, name, deviceType, ... } },
        device:  { v1: { components, batteryLevel, ... } } }

    We merge them via dict update -- same approach as ring-client-api.
    """
    result: dict[str, Any] = {}
    general = data.get("general")
    if isinstance(general, dict) and isinstance(general.get("v2"), dict):
        result.update(general["v2"])
    device = data.get("device")
    if isinstance(device, dict) and isinstance(device.get("v1"), dict):
        result.update(device["v1"])
    return result


class SmokeDetectorWebSocket:
    """WebSocket connection for a single Ring location.

    Manages the lifecycle of a WebSocket connection to Ring's servers
    for discovering and monitoring Kidde smoke detectors at a specific
    location. Handles auto-reconnect with exponential backoff.

    The aiohttp session is owned by the caller (Home Assistant's shared
    session) and is never closed here.
    """

    def __init__(
        self,
        location_id: str,
        location_name: str,
        rest_client: RingRestClient,
        session: aiohttp.ClientSession,
        on_device_update: Callable[[dict], None] | None = None,
        on_devices_discovered: Callable[[list[dict]], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        on_impulse: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        self.location_id = location_id
        self.location_name = location_name
        self._rest_client = rest_client
        self._session = session
        self._on_device_update = on_device_update
        self._on_devices_discovered = on_devices_discovered
        self._on_connection_change = on_connection_change
        self._on_impulse = on_impulse
        self._assets: list[dict] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._disconnected = False
        self._connected = False
        self._reconnect_requested = False
        self._consecutive_failures = 0
        self._seq = 1
        self._devices: list[dict] = []
        self._received_asset_lists: set[str] = set()
        self._device_future: asyncio.Future[list[dict]] | None = None
        self._message_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

    @property
    def has_assets(self) -> bool:
        """Whether this location has any Kidde smoke detector assets."""
        return len(self._assets) > 0

    @property
    def connected(self) -> bool:
        """Whether the websocket is currently established."""
        return self._connected

    async def connect(self) -> list[dict]:
        """Establish the WebSocket connection and return discovered devices.

        Raises on failure so the caller can decide how to handle it.
        Once connected, reconnects are handled internally.
        """
        if self._disconnected:
            return []
        try:
            return await self._establish()
        except BaseException:
            # Clean up a partially established connection so no message
            # task or open socket outlives a failed connect.
            await self._close_ws()
            raise

    async def _establish(self) -> list[dict]:
        """Fetch a ticket, open the websocket, and run device discovery.

        1. Request ticket from clap/tickets endpoint
        2. Filter for sensor_bluejay_* assets (key difference from ring-client-api)
        3. Connect to WebSocket
        4. Send DeviceInfoDocGetList for each asset
        5. Wait for all assets to respond with device data
        """
        ticket_url = (
            f"{APP_API_BASE}clap/tickets"
            f"?locationID={self.location_id}"
            f"&enableExtendedEmergencyCellUsage=true"
            f"&requestedTransport=ws"
        )
        ticket_response = await self._rest_client.request(ticket_url)
        assets = ticket_response.get("assets", [])

        supported_assets = [a for a in assets if is_kidde_asset(a)]
        self._assets = supported_assets
        self._received_asset_lists = set()
        self._devices = []

        if not supported_assets:
            _LOGGER.debug(
                'Location "%s": no Kidde assets found',
                self.location_name,
            )
            return []

        _LOGGER.debug(
            'Location "%s": %d websocket asset(s) -- %s',
            self.location_name,
            len(supported_assets),
            ", ".join(
                f"{a['uuid']} ({a['kind']}, {a.get('status', 'unknown')})"
                for a in supported_assets
            ),
        )

        ticket = ticket_response["ticket"]
        host = ticket_response["host"]
        ws_url = f"wss://{host}/ws?authcode={ticket}&ack=false"
        ws = await self._session.ws_connect(ws_url, heartbeat=WS_HEARTBEAT)

        if self._disconnected:
            # disconnect() ran while the handshake was in flight
            await ws.close()
            return []

        self._ws = ws
        self._consecutive_failures = 0
        self._connected = True
        _LOGGER.info(
            'WebSocket connected for location "%s"',
            self.location_name,
        )
        if self._on_connection_change:
            self._on_connection_change(True)

        loop = asyncio.get_running_loop()
        self._device_future = loop.create_future()
        self._message_task = asyncio.create_task(self._message_loop())

        for asset in supported_assets:
            await self._send_message(
                {"msg": "DeviceInfoDocGetList", "dst": asset["uuid"]}
            )

        try:
            devices = await asyncio.wait_for(
                self._device_future, timeout=DISCOVERY_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                'Timed out waiting for device list from "%s"',
                self.location_name,
            )
            devices = list(self._devices)

        _LOGGER.info(
            'Location "%s": discovered %d device(s)',
            self.location_name,
            len(devices),
        )

        return devices

    async def _message_loop(self) -> None:
        """Process incoming WebSocket messages."""
        if not self._ws:
            return

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    _LOGGER.debug(
                        'WebSocket closed/error for "%s"',
                        self.location_name,
                    )
                    break
        except asyncio.CancelledError:
            return
        except Exception as err:
            _LOGGER.debug("WebSocket message loop error: %s", err)

        self._schedule_reconnect()

    async def _handle_message(self, raw_data: str) -> None:
        """Parse and route a single WebSocket message."""
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            _LOGGER.debug("Failed to parse WebSocket message")
            return

        if not isinstance(parsed, dict):
            return

        message = parsed.get("msg")
        channel = parsed.get("channel")

        if not isinstance(message, dict):
            return

        datatype = message.get("datatype")

        # Ring server tells us to reconnect
        if datatype == "HubDisconnectionEventType":
            _LOGGER.warning(
                'Hub disconnection for "%s", reconnecting...',
                self.location_name,
            )
            self._schedule_reconnect()
            return

        msg_type = message.get("msg")
        body = message.get("body", [])
        src = message.get("src", "")
        if not isinstance(body, list):
            return

        # Initial device list response from DeviceInfoDocGetList
        if msg_type == "DeviceInfoDocGetList" and body:
            self._received_asset_lists.add(src)
            for data in body:
                if not isinstance(data, dict):
                    continue
                flat = flatten_device_data(data)
                existing = next(
                    (d for d in self._devices if d.get("zid") == flat.get("zid")),
                    None,
                )
                if existing:
                    existing.update(flat)
                else:
                    self._devices.append(flat)

            # Check if all assets have responded
            if all(a["uuid"] in self._received_asset_lists for a in self._assets):
                if self._device_future and not self._device_future.done():
                    self._device_future.set_result(list(self._devices))
                if self._on_devices_discovered:
                    self._on_devices_discovered(list(self._devices))

        # Real-time state updates (alarm triggered, battery changed, etc.)
        if (
            channel == "DataUpdate"
            and datatype == "DeviceInfoDocType"
            and body
        ):
            for data in body:
                if not isinstance(data, dict):
                    continue
                flat = flatten_device_data(data)
                if self._on_device_update:
                    self._on_device_update(flat)
                impulses = extract_impulses(data)
                if impulses:
                    # Nobody has publicly captured a real test press yet,
                    # so keep the raw payload available for diagnosis.
                    _LOGGER.debug(
                        "Impulse(s) %s for device %s, raw body: %s",
                        impulses,
                        flat.get("zid"),
                        data,
                    )
                    zid = flat.get("zid")
                    if zid and self._on_impulse:
                        self._on_impulse(zid, impulses)

    def _is_live(self) -> bool:
        """Whether the socket and message loop are actually running.

        The _connected flag alone can be stale: the message loop can die
        while a reconnect attempt is still inside its discovery wait, and
        only _close_ws resets the flag.
        """
        return (
            self._ws is not None
            and not self._ws.closed
            and self._message_task is not None
            and not self._message_task.done()
        )

    def _schedule_reconnect(self) -> None:
        """Request a reconnect, starting the loop if none is running.

        The request flag is set even when a loop is already running so a
        drop that happens during that loop's own discovery window is not
        lost (the loop re-checks the flag before declaring success).
        """
        if self._disconnected:
            return
        self._reconnect_requested = True
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff (5s -> 60s cap).

        Keeps retrying until the connection is re-established or
        disconnect() is called; a single failed attempt must never
        end monitoring for the location.
        """
        while not self._disconnected:
            self._reconnect_requested = False
            await self._close_ws()
            if self._disconnected:
                return

            self._consecutive_failures += 1
            delay = min(
                INITIAL_RECONNECT_DELAY * (2 ** (self._consecutive_failures - 1)),
                MAX_RECONNECT_DELAY,
            )

            _LOGGER.info(
                'Reconnecting for "%s" in %ds (attempt %d)',
                self.location_name,
                delay,
                self._consecutive_failures,
            )

            await asyncio.sleep(delay)

            if self._disconnected:
                return

            try:
                devices = await self._establish()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning(
                    'Reconnect attempt for "%s" failed: %s',
                    self.location_name,
                    err,
                )
                continue

            if not self._is_live() or self._reconnect_requested:
                # Either the ticket transiently reported no assets, or
                # the socket dropped again during the discovery window.
                # Keep trying rather than silently ending monitoring.
                _LOGGER.warning(
                    'Reconnect for "%s" did not come up cleanly, retrying',
                    self.location_name,
                )
                continue

            if devices and self._on_devices_discovered:
                self._on_devices_discovered(devices)
            return

    async def _send_message(self, message: dict) -> None:
        """Send a message over the WebSocket."""
        if not self._ws or self._ws.closed:
            _LOGGER.debug("Cannot send message -- websocket not open")
            return
        message["seq"] = self._seq
        self._seq += 1
        await self._ws.send_str(
            json.dumps({"channel": "message", "msg": message})
        )

    @staticmethod
    async def _cancel_and_wait(task: asyncio.Task) -> None:
        """Cancel a child task and wait for it to finish.

        Re-raises CancelledError only when the CURRENT task received a
        new cancel request during the wait. Task.cancelling() stays
        elevated for the rest of a task's life once a cancellation has
        been caught, so comparing against a snapshot is required; a bare
        cancelling() check would abort cleanup that runs after a caught
        cancellation (the setup-failure path) and leak connections.
        """
        current = asyncio.current_task()
        before = current.cancelling() if current else 0
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if current and current.cancelling() > before:
                raise

    async def _close_ws(self) -> None:
        """Close the WebSocket and stop the message loop."""
        was_connected = self._connected
        self._connected = False

        if self._message_task and not self._message_task.done():
            await self._cancel_and_wait(self._message_task)
        self._message_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if was_connected and self._on_connection_change and not self._disconnected:
            self._on_connection_change(False)

    async def disconnect(self) -> None:
        """Clean shutdown -- close the WebSocket and stop reconnecting."""
        self._disconnected = True

        if self._reconnect_task and not self._reconnect_task.done():
            await self._cancel_and_wait(self._reconnect_task)
        self._reconnect_task = None

        await self._close_ws()
