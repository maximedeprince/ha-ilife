"""ILIFE binary sensor: connectivity (online / offline)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import DOMAIN
from .entity import ILifeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ILifeOnline(c) for c in data["coordinators"].values())


class ILifeOnline(ILifeEntity, BinarySensorEntity):
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_online"

    @property
    def available(self) -> bool:
        # must stay available to report the offline state itself
        return self.coordinator.last_update_success

    @property
    def is_on(self):
        return self.coordinator.online
