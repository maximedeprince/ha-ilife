"""ILIFE switches: carpet recognition + per-day schedule enable."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN, SCHEDULE_SLOTS, modify_schedule
from .entity import ILifeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
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
