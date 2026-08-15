"""Base entity for the ILIFE Clean (Tuya) backend. Mirrors entity.py."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


def tuya_device_info(device) -> DeviceInfo:
    dev = device.device or {}
    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        manufacturer="ILIFE",
        model=dev.get("product_name") or dev.get("category") or "Vacuum",
        name=dev.get("name") or dev.get("product_name") or "ILIFE",
    )


class TuyaEntity(CoordinatorEntity):
    """Common base: entity naming + availability tied to the vacuum being online."""

    _attr_has_entity_name = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self.api = coordinator.api
        self._attr_device_info = tuya_device_info(self.api)
