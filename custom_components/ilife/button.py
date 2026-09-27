"""ILIFE buttons: directional remote control, dust-bin emptying, consumable resets.

ILIFE Clean (Tuya): the consumable and map resets only.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import ILifeError, ILifeOfflineError
from .const import BACKEND_ILIFE_CLEAN, DOMAIN, TUYA_CONSUMABLES, TUYA_DP_RESET_MAP
from .entity import ILifeEntity
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_entity import TuyaEntity

# (translation_key, icon, CleanDirection)
DIRECTION_BUTTONS = [
    ("forward", "mdi:arrow-up-bold", 1),
    ("backward", "mdi:arrow-down-bold", 2),
    ("left", "mdi:arrow-left-bold", 3),
    ("right", "mdi:arrow-right-bold", 4),
    ("rc_pause", "mdi:pause", 5),
]

# (translation_key, icon, PartsStatus field) — reset the consumable life to 100%
RESET_BUTTONS = [
    ("reset_main_brush", "mdi:brush", "MainBrushLife"),
    ("reset_side_brush", "mdi:brush-variant", "SideBrushLife"),
    ("reset_filter", "mdi:air-filter", "FilterLife"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        for coordinator in data["coordinators"].values():
            functions = coordinator.spec_functions
            for _, reset, key, icon in TUYA_CONSUMABLES.values():
                if reset in functions:
                    entities.append(TuyaResetButton(coordinator, reset, key, icon))
            if TUYA_DP_RESET_MAP in functions:
                entities.append(TuyaResetButton(
                    coordinator, TUYA_DP_RESET_MAP, "reset_map", "mdi:map-marker-remove"))
    else:
        for coordinator in data["coordinators"].values():
            entities += [ILifeDirectionButton(coordinator, *b) for b in DIRECTION_BUTTONS]
            entities.append(ILifeDustButton(coordinator))
            entities += [ILifeResetButton(coordinator, *b) for b in RESET_BUTTONS]
    async_add_entities(entities)


class _Base(ILifeEntity, ButtonEntity):
    def __init__(self, coordinator, key, icon):
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{self.api.iot_id}_{key}"
        self._attr_icon = icon


class ILifeDirectionButton(_Base):
    def __init__(self, coordinator, key, icon, direction):
        super().__init__(coordinator, key, icon)
        self._direction = direction

    async def async_press(self):
        # enter remote-control mode (WorkMode 10) then send the direction
        await self.hass.async_add_executor_job(self.api.work_mode, 10)
        await self.hass.async_add_executor_job(self.api.clean_direction, self._direction)


class ILifeDustButton(_Base):
    def __init__(self, coordinator):
        super().__init__(coordinator, "dust_collection", "mdi:delete-empty")

    async def async_press(self):
        await self.hass.async_add_executor_job(self.api.set_prop, "DustCollectionSwitch", 1, 1)


class TuyaResetButton(TuyaEntity, ButtonEntity):
    """ILIFE Clean: a one-shot reset DP (brush / filter life back to new, or the map)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, code, key, icon):
        super().__init__(coordinator)
        self._code = code
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{self.api.device_id}_{code}"
        # One click from wiping the map, rooms, walls and room schedules, with no
        # confirmation: present, but off until someone enables it on purpose.
        if code == TUYA_DP_RESET_MAP:
            self._attr_entity_registry_enabled_default = False

    async def async_press(self):
        try:
            await self.coordinator.async_write(self._code, True)
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err


class ILifeResetButton(_Base):
    """Reset a consumable wear counter (main/side brush, filter) back to 100%."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key, icon, field):
        super().__init__(coordinator, key, icon)
        self._field = field

    async def async_press(self):
        current = (self.coordinator.data or {}).get("PartsStatus") or {}
        try:
            await self.hass.async_add_executor_job(self.api.reset_part, self._field, current)
        except ILifeOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except ILifeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
