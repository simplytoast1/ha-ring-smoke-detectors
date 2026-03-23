"""Binary sensor platform for Ring Smoke Detectors.

Creates binary sensors for:
- Smoke detected (all models)
- CO detected (CO-capable models only)
"""

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    """Set up binary sensors from a config entry."""
    coordinator: RingSmokeCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = []
    for zid, device in coordinator.devices.items():
        device_type = device.get("deviceType", "")

        # All models get a smoke sensor
        entities.append(RingSmokeDetectedSensor(coordinator, zid))

        # CO models get a CO sensor
        if not is_smoke_only(device_type):
            entities.append(RingCODetectedSensor(coordinator, zid))

    async_add_entities(entities)


class RingSmokeDetectorBinarySensor(
    CoordinatorEntity[RingSmokeCoordinator], BinarySensorEntity
):
    """Base class for Ring smoke detector binary sensors."""

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


class RingSmokeDetectedSensor(RingSmokeDetectorBinarySensor):
    """Binary sensor for smoke detection."""

    _attr_device_class = BinarySensorDeviceClass.SMOKE
    _attr_name = "Smoke"

    def __init__(
        self, coordinator: RingSmokeCoordinator, zid: str
    ) -> None:
        super().__init__(coordinator, zid, "smoke")

    @property
    def is_on(self) -> bool:
        """Return true if smoke is detected.

        Checks both the components structure and legacy flat fields
        for compatibility across firmware versions.
        """
        data = self._device_data
        components = data.get("components", {})
        smoke = data.get("smoke", {})
        status = (
            smoke.get("alarmStatus")
            or (components.get("alarm.smoke", {}) or {}).get("alarmStatus")
        )
        return status == "active"


class RingCODetectedSensor(RingSmokeDetectorBinarySensor):
    """Binary sensor for carbon monoxide detection."""

    _attr_device_class = BinarySensorDeviceClass.CO
    _attr_name = "Carbon monoxide"

    def __init__(
        self, coordinator: RingSmokeCoordinator, zid: str
    ) -> None:
        super().__init__(coordinator, zid, "co")

    @property
    def is_on(self) -> bool:
        """Return true if CO is detected.

        Checks both the components structure and legacy flat fields
        for compatibility across firmware versions.
        """
        data = self._device_data
        components = data.get("components", {})
        co = data.get("co", {})
        status = (
            co.get("alarmStatus")
            or (components.get("alarm.co", {}) or {}).get("alarmStatus")
        )
        return status == "active"
