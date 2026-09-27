"""ILIFE binary sensors: connectivity (online / offline); ILIFE Clean also a problem
sensor naming the robot's faults."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import BACKEND_ILIFE_CLEAN, DOMAIN, TUYA_DP_FAULT
from .entity import ILifeEntity
from .tuya_entity import TuyaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        entities = []
        for coordinator in data["coordinators"].values():
            entities.append(TuyaOnline(coordinator))
            if TUYA_DP_FAULT in (coordinator.data or {}):
                entities.append(TuyaProblem(coordinator))
        async_add_entities(entities)
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


class TuyaProblem(TuyaEntity, BinarySensorEntity):
    """On while the robot reports a fault; `faults` names them (e.g. "stuck_fault").

    `fault` is a bitmap; the names come from the product's thing model, which lists
    one label per bit.
    """

    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_problem"
        self._labels = (coordinator.model.get(TUYA_DP_FAULT) or {}).get("label") or []

    @property
    def _bits(self) -> int:
        value = (self.coordinator.data or {}).get(TUYA_DP_FAULT)
        return value if isinstance(value, int) else 0

    @property
    def is_on(self):
        return self._bits != 0

    @property
    def extra_state_attributes(self):
        bits = self._bits
        return {
            "fault_code": bits,
            "faults": [
                self._labels[bit] if bit < len(self._labels) else f"bit_{bit}"
                for bit in range(bits.bit_length()) if bits >> bit & 1
            ],
        }


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
