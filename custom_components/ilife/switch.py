"""ILIFE switches: carpet recognition + per-day schedule enable.

Also covers ILIFE Clean (Tuya): a generic switch for any Boolean-type DP the device
advertises that we don't have a specific mapping for (e.g. a mop or self-empty toggle,
under whatever code name this unit actually uses for it).
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import (
    BACKEND_ILIFE_CLEAN,
    DOMAIN,
    SCHEDULE_SLOTS,
    TUYA_SETTING_SWITCHES,
    modify_schedule,
)
from .entity import ILifeEntity
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_dynamic import unknown_functions
from .tuya_entity import TuyaEntity

# Thing-model Boolean DPs worth a switch: {code: (translation key, icon)}.
MODEL_SWITCHES = {
    "y_mop": ("y_mop", "mdi:alpha-y-circle-outline"),  # Y-shaped mopping pattern
    "auto_boost": ("carpet_boost", "mdi:rug"),        # more suction on carpet
}


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        for coordinator in data["coordinators"].values():
            for code in unknown_functions(coordinator.spec, coordinator.data or {},
                                          "Boolean"):
                entities.append(TuyaGenericSwitch(coordinator, code))
            for code, (key, icon) in TUYA_SETTING_SWITCHES.items():
                if code in coordinator.spec_functions:
                    entities.append(TuyaSettingSwitch(coordinator, code, key, icon))
            for code, (key, icon) in MODEL_SWITCHES.items():
                meta = coordinator.model.get(code) or {}
                if meta.get("access") == "rw" and meta.get("type") == "bool" \
                        and code not in coordinator.spec_functions:
                    entities.append(TuyaModelSwitch(coordinator, code, key, icon))
    else:
        for coordinator in data["coordinators"].values():
            entities.append(ILifeCarpetSwitch(coordinator))
            entities += [ILifeScheduleSwitch(coordinator, n) for n in SCHEDULE_SLOTS]
    async_add_entities(entities)


class ILifeCarpetSwitch(ILifeEntity, SwitchEntity):
    _attr_translation_key = "carpet"
    _attr_icon = "mdi:rug"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_carpet"

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("CarpetControl"))

    async def _set(self, value):
        await self.hass.async_add_executor_job(self.api.set_prop, "CarpetControl", value, None, False)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(1)

    async def async_turn_off(self, **kwargs):
        await self._set(0)


class TuyaGenericSwitch(TuyaEntity, SwitchEntity):
    """Any Boolean-type DP the device advertises that isn't already a dedicated entity."""

    _attr_icon = "mdi:toggle-switch-outline"

    def __init__(self, coordinator, code):
        super().__init__(coordinator)
        self._code = code
        self._attr_name = code.replace("_", " ").title()
        self._attr_unique_id = f"{self.api.device_id}_{code}"

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get(self._code))

    async def _set(self, value):
        try:
            await self.hass.async_add_executor_job(self.api.send, self._code, value)
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class TuyaModelSwitch(TuyaEntity, SwitchEntity):
    """A Boolean DP that only the thing model exposes (see MODEL_SWITCHES)."""

    def __init__(self, coordinator, code, key, icon):
        super().__init__(coordinator)
        self._code = code
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{self.api.device_id}_{code}"

    @property
    def is_on(self):
        value = self.coordinator.properties.get(self._code)
        return None if value is None else bool(value)

    async def _set(self, value):
        try:
            await self.coordinator.async_write(self._code, value)
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class TuyaSettingSwitch(TuyaModelSwitch):
    """A standard Boolean DP with a known meaning (see TUYA_SETTING_SWITCHES). Same
    unique_id as the generic switch it replaces, so the entity is renamed, not
    duplicated."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get(self._code)
        return None if value is None else bool(value)


class ILifeScheduleSwitch(ILifeEntity, SwitchEntity):
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, n):
        super().__init__(coordinator)
        self._n = n
        self._attr_translation_key = f"schedule_{n}_enable"
        self._attr_unique_id = f"{self.api.iot_id}_schedule{n}_enable"

    @property
    def is_on(self):
        sched = (self.coordinator.data or {}).get(f"Schedule{self._n}") or {}
        return bool(sched.get("ScheduleEnable"))

    async def _set(self, enable):
        struct = modify_schedule(self.coordinator.data, self._n, ScheduleEnable=enable)
        await self.hass.async_add_executor_job(self.api.set_schedule, self._n, struct)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(1)

    async def async_turn_off(self, **kwargs):
        await self._set(0)
