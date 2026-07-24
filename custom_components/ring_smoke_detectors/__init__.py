"""Ring Smoke Detectors integration for Home Assistant.

Exposes Kidde/Ring smart smoke and CO detectors as Home Assistant entities.
These are WiFi-only, hubless models not supported by the standard Ring integration.

Uses WebSocket connections to Ring's servers for real-time alarm state,
bypassing the hub requirement that blocks standard Ring integrations.
"""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import RingSmokeConfigEntry, RingSmokeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: RingSmokeConfigEntry
) -> bool:
    """Set up Ring Smoke Detectors from a config entry."""
    coordinator = RingSmokeCoordinator(hass, entry)

    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Setup failures (including cancellation) never reach
        # async_unload_entry, so close any websockets that were
        # established before the failure.
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: RingSmokeConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()

    return unload_ok
