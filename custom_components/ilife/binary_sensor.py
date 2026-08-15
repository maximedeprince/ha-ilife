"""ILIFE binary sensor: connectivity (online / offline)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import BACKEND_ILIFE_CLEAN, DOMAIN
from .entity import ILifeEntity
from .tuya_entity import TuyaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        async_add_entities(TuyaOnline(c) for c in data["coordinators"].values())
    else:
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


class TuyaOnline(TuyaEntity, BinarySensorEntity):
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_online"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_on(self):
        return self.coordinator.online
