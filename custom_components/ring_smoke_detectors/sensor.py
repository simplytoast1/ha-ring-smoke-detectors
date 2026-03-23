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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, CONCENTRATION_PARTS_PER_MILLION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RingSmokeCoordinator
from .ring_api.websocket import is_smoke_only

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: RingSmokeCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for zid, device in coordinator.devices.items():
        device_type = device.get("deviceType", "")

        # All models get a battery sensor
        entities.append(RingBatteryLevelSensor(coordinator, zid))

        # CO models get a CO PPM level sensor
        if not is_smoke_only(device_type):
            entities.append(RingCOLevelSensor(coordinator, zid))

    async_add_entities(entities)


class RingSmokeDetectorSensor(
    CoordinatorEntity[RingSmokeCoordinator], SensorEntity
):
    """Base class for Ring smoke detector sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RingSmokeCoordinator,
        zid: str,
        sensor_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._zid = zid
        self._attr_unique_id = f"{zid}_{sensor_type}"
        device = coordinator.devices.get(zid, {})
        self._attr_device_info = {
            "identifiers": {(DOMAIN, zid)},
            "name": device.get("name", "Smoke Detector"),
            "manufacturer": "Kidde",
            "model": self._get_model_name(device.get("deviceType", "")),
            "serial_number": device.get("serialNumber", zid),
        }

    @staticmethod
    def _get_model_name(device_type: str) -> str:
        if "sensor_bluejay_wsc" in device_type:
            return "Smart Smoke + CO Alarm (Wired)"
        if "sensor_bluejay_ws" in device_type:
            return "Smart Smoke Alarm (Wired)"
        if "sensor_bluejay_sc" in device_type:
            return "Smart Smoke + CO Alarm (Battery)"
        return device_type

    @property
    def _device_data(self) -> dict[str, Any]:
        return self.coordinator.devices.get(self._zid, {})


class RingBatteryLevelSensor(RingSmokeDetectorSensor):
    """Sensor for battery level percentage."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Battery"

    def __init__(
        self, coordinator: RingSmokeCoordinator, zid: str
    ) -> None:
        super().__init__(coordinator, zid, "battery")

    @property
    def native_value(self) -> int:
        """Return the battery level."""
        return self._device_data.get("batteryLevel", 100)

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


class RingCOLevelSensor(RingSmokeDetectorSensor):
    """Sensor for CO level in parts per million."""

    _attr_device_class = SensorDeviceClass.CO
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "CO level"

    def __init__(
        self, coordinator: RingSmokeCoordinator, zid: str
    ) -> None:
        super().__init__(coordinator, zid, "co_level")

    @property
    def native_value(self) -> int:
        """Return the CO level in PPM."""
        components = self._device_data.get("components", {})
        co_level = components.get("co.level", {})
        return co_level.get("reading", 0)
