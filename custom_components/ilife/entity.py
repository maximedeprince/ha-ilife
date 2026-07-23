"""Base entity for the ILIFE integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


def device_info(device) -> DeviceInfo:
    dev = device.device or {}
    return DeviceInfo(
        identifiers={(DOMAIN, device.iot_id)},
        manufacturer="ILIFE",
        model=dev.get("productName") or "Vacuum",
        name=dev.get("nickName") or dev.get("productName") or "ILIFE",
    )


class ILifeEntity(CoordinatorEntity):
    """Common base: entity naming + availability tied to the vacuum being online."""

    _attr_has_entity_name = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self.api = coordinator.api
        self._attr_device_info = device_info(self.api)

    @property
    def available(self) -> bool:
        # available unless the coordinator failed OR the vacuum is explicitly offline
        return super().available and self.coordinator.online is not False
