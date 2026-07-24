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

    # NOTE: availability is intentionally NOT gated on the cloud "online" flag.
    # ILIFE vacuums often report offline while docked (Wi-Fi sleep) even though the
    # cloud still returns valid cached state, so gating would make everything look
    # unavailable most of the time. Online status is exposed via the connectivity
    # binary sensor and the card instead. Availability = coordinator success only
    # (inherited from CoordinatorEntity).
