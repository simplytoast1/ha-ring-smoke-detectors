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
"""

import asyncio
import json
import logging
from typing import Any, Callable

import aiohttp

from ..const import APP_API_BASE
from .auth import RingRestClient

_LOGGER = logging.getLogger(__name__)

MAX_RECONNECT_DELAY = 60
INITIAL_RECONNECT_DELAY = 5


def is_kidde_asset(asset: dict) -> bool:
    """Check if a WebSocket ticket asset is a Kidde smoke detector."""
    return asset.get("kind", "").startswith("sensor_bluejay")


def is_kidde_device_type(device_type: str) -> bool:
    """Check if a WebSocket deviceType is a Kidde smoke detector."""
    return "sensor_bluejay" in device_type


def is_smoke_only(device_type: str) -> bool:
    """Check if a device is smoke-only (no CO sensor)."""
    return device_type in (
        "sensor_bluejay_ws",
        "comp.bluejay.sensor_bluejay_ws",
    )


def flatten_device_data(data: dict) -> dict:
    """Flatten nested WebSocket device data into a single dict.

    WebSocket responses contain device data split across two objects:
      { general: { v2: { zid, name, deviceType, ... } },
        device:  { v1: { components, batteryLevel, ... } } }

    We merge them via dict update -- same approach as ring-client-api.
    """
    result: dict[str, Any] = {}
    if "general" in data and "v2" in data["general"]:
        result.update(data["general"]["v2"])
    if "device" in data and "v1" in data["device"]:
        result.update(data["device"]["v1"])
    return result


class SmokeDetectorWebSocket:
    """WebSocket connection for a single Ring location.

    Manages the lifecycle of a WebSocket connection to Ring's servers
    for discovering and monitoring Kidde smoke detectors at a specific
    location. Handles auto-reconnect with exponential backoff.
    """

    def __init__(
        self,
        location_id: str,
        location_name: str,
        rest_client: RingRestClient,
        on_device_update: Callable[[dict], None] | None = None,
        on_devices_discovered: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self.location_id = location_id
        self.location_name = location_name
        self._rest_client = rest_client
        self._on_device_update = on_device_update
        self._on_devices_discovered = on_devices_discovered
        self._assets: list[dict] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_session: aiohttp.ClientSession | None = None
        self._disconnected = False
        self._consecutive_failures = 0
        self._seq = 1
        self._devices: list[dict] = []
        self._received_asset_lists: set[str] = set()
        self._device_future: asyncio.Future[list[dict]] | None = None
        self._message_task: asyncio.Task | None = None

    @property
    def has_assets(self) -> bool:
        """Whether this location has any Kidde smoke detector assets."""
        return len(self._assets) > 0

    async def connect(self) -> list[dict]:
        """Establish the WebSocket connection and return discovered devices.

        1. Request ticket from clap/tickets endpoint
        2. Filter for sensor_bluejay_* assets (key difference from ring-client-api)
        3. Connect to WebSocket
        4. Send DeviceInfoDocGetList for each asset
        5. Wait for all assets to respond with device data
        """
        if self._disconnected:
            return []

        try:
            ticket_url = (
                f"{APP_API_BASE}clap/tickets"
                f"?locationID={self.location_id}"
                f"&enableExtendedEmergencyCellUsage=true"
                f"&requestedTransport=ws"
            )
            ticket_response = await self._rest_client.request(ticket_url)
            assets = ticket_response.get("assets", [])
            ticket = ticket_response["ticket"]
            host = ticket_response["host"]

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

            ws_url = f"wss://{host}/ws?authcode={ticket}&ack=false"
            self._ws_session = aiohttp.ClientSession()
            self._ws = await self._ws_session.ws_connect(ws_url)

            self._consecutive_failures = 0
            _LOGGER.info(
                'WebSocket connected for location "%s"',
                self.location_name,
            )

            for asset in supported_assets:
                await self._send_message(
                    {"msg": "DeviceInfoDocGetList", "dst": asset["uuid"]}
                )

            loop = asyncio.get_running_loop()
            self._device_future = loop.create_future()

            self._message_task = asyncio.create_task(self._message_loop())

            try:
                devices = await asyncio.wait_for(self._device_future, timeout=15)
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

        except Exception as err:
            _LOGGER.error(
                'WebSocket connect failed for "%s": %s',
                self.location_name,
                err,
            )
            self._consecutive_failures += 1
            return []

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

        if not self._disconnected:
            asyncio.create_task(self._reconnect())

    async def _handle_message(self, raw_data: str) -> None:
        """Parse and route a single WebSocket message."""
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            _LOGGER.debug("Failed to parse WebSocket message")
            return

        message = parsed.get("msg")
        channel = parsed.get("channel")

        if not message:
            return

        datatype = message.get("datatype")

        # Ring server tells us to reconnect
        if datatype == "HubDisconnectionEventType":
            _LOGGER.warning(
                'Hub disconnection for "%s", reconnecting...',
                self.location_name,
            )
            asyncio.create_task(self._reconnect())
            return

        msg_type = message.get("msg")
        body = message.get("body", [])
        src = message.get("src", "")

        # Initial device list response from DeviceInfoDocGetList
        if msg_type == "DeviceInfoDocGetList" and body:
            self._received_asset_lists.add(src)
            for data in body:
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
                flat = flatten_device_data(data)
                if self._on_device_update:
                    self._on_device_update(flat)

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff (5s -> 60s cap)."""
        if self._disconnected:
            return

        await self._close_ws()

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

        if not self._disconnected:
            devices = await self.connect()
            if devices and self._on_devices_discovered:
                self._on_devices_discovered(devices)

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

    async def _close_ws(self) -> None:
        """Close the WebSocket and its HTTP session."""
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass
        self._message_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
        self._ws_session = None

    async def disconnect(self) -> None:
        """Clean shutdown -- close the WebSocket and stop reconnecting."""
        self._disconnected = True
        await self._close_ws()
