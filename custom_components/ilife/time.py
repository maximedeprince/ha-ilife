"""ILIFE schedule time (one time entity per weekday)."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity

from .const import DOMAIN, SCHEDULE_SLOTS, modify_schedule
from .entity import ILifeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for coordinator in data["coordinators"].values():
        entities += [ILifeScheduleTime(coordinator, n) for n in SCHEDULE_SLOTS]
    async_add_entities(entities)


class ILifeScheduleTime(ILifeEntity, TimeEntity):
    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator, n):
        super().__init__(coordinator)
        self._n = n
        self._attr_translation_key = f"schedule_{n}_time"
        self._attr_unique_id = f"{self.api.iot_id}_schedule{n}_time"

    @property
    def native_value(self):
        sched = (self.coordinator.data or {}).get(f"Schedule{self._n}")
        if not isinstance(sched, dict):
            return None
        h = sched.get("ScheduleHour")
        m = sched.get("ScheduleMinutes")
        if h is None or m is None:
            return None
        try:
            return time(hour=int(h), minute=int(m))
        except (ValueError, TypeError):
            return None

    async def async_set_value(self, value: time):
        struct = modify_schedule(
            self.coordinator.data, self._n,
            ScheduleHour=value.hour, ScheduleMinutes=value.minute,
        )
        await self.hass.async_add_executor_job(self.api.set_schedule, self._n, struct)
        await self.coordinator.async_request_refresh()
