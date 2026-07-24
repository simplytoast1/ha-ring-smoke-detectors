"""Shared entity base class for Ring Smoke Detectors."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, model_name
from .coordinator import RingSmokeCoordinator


class RingSmokeDetectorEntity(CoordinatorEntity[RingSmokeCoordinator]):
    """Base class for all Ring smoke detector entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RingSmokeCoordinator,
        zid: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._zid = zid
        self._attr_unique_id = f"{zid}_{key}"
        device = coordinator.devices.get(zid, {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, zid)},
            name=device.get("name", "Smoke Detector"),
            manufacturer="Kidde",
            model=model_name(device.get("deviceType", "")),
            serial_number=device.get("serialNumber", zid),
        )

    @property
    def _device_data(self) -> dict[str, Any]:
        return self.coordinator.devices.get(self._zid, {})

    @property
    def available(self) -> bool:
        """Unavailable while the location's websocket is down.

        Without this, a dead connection would be indistinguishable from
        "no smoke", which is the worst failure mode for a safety sensor.
        """
        return super().available and self.coordinator.is_device_available(
            self._zid
        )
