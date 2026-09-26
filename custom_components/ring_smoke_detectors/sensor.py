"""Sensor platform for Ring Smoke Detectors.

Creates sensors for:
- Battery level (all models) -- an approximate percentage mapped from the
  categorical battery status the detector reports
- CO level in PPM (CO-capable models only)
"""

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION, PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import BATTERY_STATUS_PERCENT
from .coordinator import RingSmokeConfigEntry, RingSmokeCoordinator
from .entity import RingSmokeDetectorEntity
from .ring_api.websocket import is_smoke_only

_LOGGER = logging.getLogger(__name__)

# Battery-status spellings already warned about, to avoid log spam
_WARNED_BATTERY_STATUS: set[str] = set()


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

            # All models report last communication date/time
            entities.append(RingLastCommDateTimeSensor(coordinator, zid))

            # All models report a battery status
            entities.append(RingBatterySensor(coordinator, zid))

            # All models report WiFi signal strength
            entities.append(RingWifiSignalStrengthSensor(coordinator, zid))

            # CO models get a CO PPM level sensor
            if not is_smoke_only(device.get("deviceType", "")):
                entities.append(RingCOLevelSensor(coordinator, zid))
        if entities:
            async_add_entities(entities)

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class RingBatterySensor(RingSmokeDetectorEntity, SensorEntity):
    """Battery level, mapped from the detector's categorical battery status.

    These detectors report batteryStatus (e.g. "full", "low"), not a numeric
    percentage, so the level is an approximation of that category. The raw
    status is exposed as an attribute. A missing or unrecognized status
    reports unknown, never a fabricated number.
    """

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "battery")

    @property
    def native_value(self) -> int | None:
        """Return an approximate battery percentage from batteryStatus."""
        status = self._device_data.get("batteryStatus")
        if status is None:
            return None
        if status not in BATTERY_STATUS_PERCENT:
            if status not in _WARNED_BATTERY_STATUS:
                _WARNED_BATTERY_STATUS.add(status)
                _LOGGER.warning(
                    "Unrecognized batteryStatus %r; reporting unknown. Please "
                    "report this value on the issue tracker so it can be mapped",
                    status,
                )
            return None
        return BATTERY_STATUS_PERCENT[status]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw battery status plus AC, comm, and tamper status."""
        data = self._device_data
        attrs: dict[str, Any] = {}
        status = data.get("batteryStatus")
        if status is not None:
            attrs["battery_status"] = status
        for attr, key in (
            ("ac_status", "acStatus"),
            ("comm_status", "commStatus"),
            ("tamper_status", "tamperStatus"),
        ):
            if key in data:
                attrs[attr] = data[key]
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


class RingWifiSignalStrengthSensor(RingSmokeDetectorEntity, SensorEntity):
    """Sensor for WiFi signal strength."""

    _attr_name = "WiFi Signal Strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "wifi_signal_strength")

    @property
    def native_value(self) -> int | None:
        """Return the WiFi signal strength in dBm, or None when no reading exists."""
        components = self._device_data.get("components") or {}
        co_level = components.get("networks:wlan0") or {}
        return co_level.get("rssi")


class RingLastCommDateTimeSensor(RingSmokeDetectorEntity, SensorEntity):
    """Sensor for last communication date/time."""

    _attr_name = "Last Communication Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: RingSmokeCoordinator, zid: str) -> None:
        super().__init__(coordinator, zid, "last_comm_time")

    @property
    def native_value(self) -> int | None:
        """Return the last seen timestamp, or None when no reading exists."""
        last_comm_time_ms = self._device_data.get("lastCommTime")
        last_comm_time_s = last_comm_time_ms / 1000.0
        return dt_util.utc_from_timestamp(last_comm_time_s)
