"""Binary sensor platform for Ring Smoke Detectors.

Creates binary sensors for:
- Smoke detected (all models)
- CO detected (CO-capable models only)
"""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RingSmokeConfigEntry, RingSmokeCoordinator
from .entity import RingSmokeDetectorEntity
from .ring_api.websocket import is_smoke_only

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RingSmokeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors, including for devices discovered later."""
    coordinator = entry.runtime_data
    known_zids: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        entities: list[BinarySensorEntity] = []
        for zid, device in coordinator.devices.items():
            if zid in known_zids:
                continue
            known_zids.add(zid)

            # All models get a smoke sensor
            entities.append(RingSmokeDetectedSensor(coordinator, zid))

            # CO models get a CO sensor
            if not is_smoke_only(device.get("deviceType", "")):
                entities.append(RingCODetectedSensor(coordinator, zid))
        if entities:
            async_add_entities(entities)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class RingSmokeDetectedSensor(RingSmokeDetectorEntity, BinarySensorEntity):
    """Binary sensor for smoke detection."""

    _attr_device_class = BinarySensorDeviceClass.SMOKE

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "smoke")

    @property
    def is_on(self) -> bool:
        """Return true if smoke is detected.

        Checks both the components structure and legacy flat fields
        for compatibility across firmware versions.
        """
        data = self._device_data
        components = data.get("components") or {}
        smoke = data.get("smoke") or {}
        status = (
            smoke.get("alarmStatus")
            or (components.get("alarm.smoke") or {}).get("alarmStatus")
        )
        return status == "active"


class RingCODetectedSensor(RingSmokeDetectorEntity, BinarySensorEntity):
    """Binary sensor for carbon monoxide detection."""

    _attr_device_class = BinarySensorDeviceClass.CO

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "co")

    @property
    def is_on(self) -> bool:
        """Return true if CO is detected.

        Checks both the components structure and legacy flat fields
        for compatibility across firmware versions.
        """
        data = self._device_data
        components = data.get("components") or {}
        co = data.get("co") or {}
        status = (
            co.get("alarmStatus")
            or (components.get("alarm.co") or {}).get("alarmStatus")
        )
        return status == "active"
