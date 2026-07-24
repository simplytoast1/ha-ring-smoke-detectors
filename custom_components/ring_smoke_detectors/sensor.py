"""Sensor platform for Ring Smoke Detectors.

Creates sensors for:
- Battery level (all models)
- CO level in PPM (CO-capable models only)
"""

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, CONCENTRATION_PARTS_PER_MILLION
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
    """Set up sensors, including for devices discovered later."""
    coordinator = entry.runtime_data
    known_zids: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        entities: list[SensorEntity] = []
        for zid, device in coordinator.devices.items():
            if zid in known_zids:
                continue
            known_zids.add(zid)

            # All models get a battery sensor (wired models have a backup)
            entities.append(RingBatteryLevelSensor(coordinator, zid))

            # CO models get a CO PPM level sensor
            if not is_smoke_only(device.get("deviceType", "")):
                entities.append(RingCOLevelSensor(coordinator, zid))
        if entities:
            async_add_entities(entities)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class RingBatteryLevelSensor(RingSmokeDetectorEntity, SensorEntity):
    """Sensor for battery level percentage."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "battery")

    @property
    def native_value(self) -> int | None:
        """Return the battery level, or None when the device omits it."""
        return self._device_data.get("batteryLevel")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return battery status and charging state."""
        data = self._device_data
        attrs: dict[str, Any] = {}
        if "batteryStatus" in data:
            attrs["battery_status"] = data["batteryStatus"]
        if "acStatus" in data:
            attrs["ac_status"] = data["acStatus"]
        return attrs


class RingCOLevelSensor(RingSmokeDetectorEntity, SensorEntity):
    """Sensor for CO level in parts per million."""

    _attr_device_class = SensorDeviceClass.CO
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "co_level"

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "co_level")

    @property
    def native_value(self) -> int | None:
        """Return the CO level in PPM, or None when no reading exists."""
        components = self._device_data.get("components") or {}
        co_level = components.get("co.level") or {}
        return co_level.get("reading")
