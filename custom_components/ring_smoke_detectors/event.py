"""Event platform for Ring Smoke Detectors.

Exposes a "Test button" event entity per detector that fires when the
physical test button is pressed. Detection is driven by the coordinator
from websocket impulse events and pushToTest component changes.
"""

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_TYPE_TEST, signal_test_event
from .coordinator import RingSmokeConfigEntry, RingSmokeCoordinator
from .entity import RingSmokeDetectorEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RingSmokeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up event entities, including for devices discovered later."""
    coordinator = entry.runtime_data
    known_zids: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        entities = [
            RingTestButtonEvent(coordinator, zid)
            for zid in coordinator.devices
            if zid not in known_zids
        ]
        known_zids.update(coordinator.devices)
        if entities:
            async_add_entities(entities)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class RingTestButtonEvent(RingSmokeDetectorEntity, EventEntity):
    """Event entity that fires when the detector's test button is pressed."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [EVENT_TYPE_TEST]
    _attr_translation_key = "test_button"

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "test_button")

    async def async_added_to_hass(self) -> None:
        """Subscribe to test events for this device."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_test_event(self.coordinator.entry.entry_id, self._zid),
                self._handle_test_event,
            )
        )

    @callback
    def _handle_test_event(self) -> None:
        self._trigger_event(EVENT_TYPE_TEST)
        self.async_write_ha_state()
