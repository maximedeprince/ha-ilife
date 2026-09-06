"""ILIFE selects: water level (low nibble of VacWateState) and start cleaning mode.

Also covers ILIFE Clean (Tuya): a dedicated "mode" select plus a generic select for any
other Enum-type DP the device advertises that we don't have a specific mapping for.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    BACKEND_ILIFE_CLEAN,
    CLEAN_MODES,
    CLEANING_MODES,
    DOMAIN,
    TUYA_DP_MODE,
    WATER_LEVELS,
    clean_mode_label,
    pack_vws,
    water_label,
)
from .entity import ILifeEntity
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_dynamic import unknown_functions
from .tuya_entity import TuyaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        for coordinator in data["coordinators"].values():
            functions = coordinator.spec_functions
            if TUYA_DP_MODE in functions:
                entities.append(TuyaModeSelect(coordinator))
            for code, meta in unknown_functions(
                coordinator.spec, coordinator.data or {}, "Enum").items():
                entities.append(TuyaGenericSelect(coordinator, code, meta))
    else:
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


class _TuyaSelectBase(TuyaEntity, SelectEntity):
    async def _send(self, code, value):
        try:
            await self.hass.async_add_executor_job(self.api.send, code, value)
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class TuyaModeSelect(_TuyaSelectBase):
    _attr_translation_key = "cleaning_mode"
    _attr_icon = "mdi:vector-square"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_mode"
        self._attr_options = [
            str(v) for v in
            ((coordinator.spec_functions.get(TUYA_DP_MODE) or {}).get("values") or {}).get(
                "range") or []
        ]

    @property
    def current_option(self):
        v = (self.coordinator.data or {}).get(TUYA_DP_MODE)
        return str(v) if v is not None else None

    async def async_select_option(self, option):
        await self._send(TUYA_DP_MODE, option)


class TuyaGenericSelect(_TuyaSelectBase):
    """Any other Enum-type DP the device advertises (e.g. suction/water level under
    whatever code name this specific unit actually uses for it)."""

    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator, code, meta):
        super().__init__(coordinator)
        self._code = code
        self._attr_name = code.replace("_", " ").title()
        self._attr_unique_id = f"{self.api.device_id}_{code}"
        self._attr_options = [str(v) for v in (meta.get("values") or {}).get("range") or []]

    @property
    def current_option(self):
        v = (self.coordinator.data or {}).get(self._code)
        return str(v) if v is not None else None

    async def async_select_option(self, option):
        await self._send(self._code, option)
