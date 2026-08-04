"""ILIFE buttons: directional remote control, dust-bin emptying, consumable resets."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import ILifeError, ILifeOfflineError
from .const import DOMAIN
from .entity import ILifeEntity

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
