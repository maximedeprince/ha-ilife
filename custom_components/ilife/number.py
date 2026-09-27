"""ILIFE Clean (Tuya) numbers: voice volume and how often the dock empties the bin."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import BACKEND_ILIFE_CLEAN, DOMAIN, TUYA_DP_DUST_FREQUENCY, TUYA_DP_VOLUME
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_entity import TuyaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.get("backend") != BACKEND_ILIFE_CLEAN:
        return
    entities = []
    for coordinator in data["coordinators"].values():
        functions = coordinator.spec_functions
        if TUYA_DP_VOLUME in functions:
            entities.append(TuyaVolume(coordinator))
        if TUYA_DP_DUST_FREQUENCY in functions:
            entities.append(TuyaDustFrequency(coordinator))
    async_add_entities(entities)


class _TuyaNumber(TuyaEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _code: str
    _scale = 1

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_{self._code}_number"

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get(self._code)
        return None if value is None else value * self._scale

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_write(self._code, round(value / self._scale))
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err


class TuyaVolume(_TuyaNumber):
    """Voice volume. The DP counts 0-10; shown in %, as the app does."""

    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 10
    _attr_mode = NumberMode.SLIDER
    _code = TUYA_DP_VOLUME
    _scale = 10


class TuyaDustFrequency(_TuyaNumber):
    """How many cleans between two bin emptyings at the dock. The DP advertises up to
    99999, but the robot's range is 0-4 (as tuya-local maps it for the A30 Pro)."""

    _attr_translation_key = "auto_empty_frequency"
    _attr_icon = "mdi:delete-clock"
    _attr_native_min_value = 0
    _attr_native_max_value = 4
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _code = TUYA_DP_DUST_FREQUENCY
