"""ILIFE selects: water level (low nibble of VacWateState) and start cleaning mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CLEAN_MODES,
    CLEANING_MODES,
    DOMAIN,
    WATER_LEVELS,
    clean_mode_label,
    pack_vws,
    water_label,
)
from .entity import ILifeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for coordinator in data["coordinators"].values():
        entities += [ILifeWaterSelect(coordinator), ILifeCleanModeSelect(coordinator)]
    async_add_entities(entities)


class ILifeWaterSelect(ILifeEntity, SelectEntity):
    _attr_translation_key = "water_level"
    _attr_icon = "mdi:water"
    _attr_options = list(WATER_LEVELS)

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_water"

    @property
    def current_option(self):
        return water_label((self.coordinator.data or {}).get("VacWateState"))

    async def async_select_option(self, option):
        cur = (self.coordinator.data or {}).get("VacWateState")
        await self.hass.async_add_executor_job(
            self.api.set_prop, "VacWateState", pack_vws(cur, water=option), None, False)
        await self.coordinator.async_request_refresh()


class ILifeCleanModeSelect(ILifeEntity, SelectEntity, RestoreEntity):
    """Cleaning mode used on START (S-shape / Auto)."""

    _attr_translation_key = "cleaning_mode"
    _attr_icon = "mdi:vector-square"
    _attr_options = list(CLEAN_MODES)

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_clean_mode"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_MODES:
            self.coordinator.clean_mode = CLEAN_MODES[last.state]

    @property
    def current_option(self):
        # reflect the live mode while cleaning, else the remembered preference
        wm = (self.coordinator.data or {}).get("WorkMode")
        return clean_mode_label(wm) or clean_mode_label(self.coordinator.clean_mode)

    async def async_select_option(self, option):
        mode = CLEAN_MODES[option]
        self.coordinator.clean_mode = mode
        self.async_write_ha_state()
        if (self.coordinator.data or {}).get("WorkMode") in CLEANING_MODES:
            await self.hass.async_add_executor_job(self.api.work_mode, mode)
            await self.coordinator.async_request_refresh()
