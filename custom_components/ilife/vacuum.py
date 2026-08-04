"""ILIFE vacuum entity."""
from __future__ import annotations

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

from .api import ILifeError, ILifeOfflineError
from .const import (
    CLEANING_MODES,
    DOCKED_MODES,
    DOMAIN,
    PAUSED_MODES,
    RETURNING_MODES,
    SUCTION_LEVELS,
    pack_vws,
    suction_label,
)
from .entity import ILifeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ILifeVacuum(c) for c in data["coordinators"].values())


class ILifeVacuum(ILifeEntity, StateVacuumEntity):
    _attr_name = None  # main entity: uses the device name
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.LOCATE
    )
    _attr_fan_speed_list = list(SUCTION_LEVELS)

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_vacuum"

    @property
    def activity(self):
        wm = (self.coordinator.data or {}).get("WorkMode")
        if wm in CLEANING_MODES:
            return VacuumActivity.CLEANING
        if wm in RETURNING_MODES:
            return VacuumActivity.RETURNING
        if wm in DOCKED_MODES:
            return VacuumActivity.DOCKED
        if wm in PAUSED_MODES:
            return VacuumActivity.PAUSED
        return VacuumActivity.IDLE

    @property
    def fan_speed(self):
        return suction_label((self.coordinator.data or {}).get("VacWateState"))

    async def _cmd(self, fn, *args):
        try:
            await self.hass.async_add_executor_job(fn, *args)
        except ILifeOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except ILifeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_start(self):
        await self._cmd(self.api.work_mode, self.coordinator.clean_mode)

    async def async_pause(self):
        await self._cmd(self.api.work_mode, 12)

    async def async_stop(self, **kwargs):
        await self._cmd(self.api.work_mode, 2)

    async def async_return_to_base(self, **kwargs):
        await self._cmd(self.api.work_mode, 8)

    async def async_locate(self, **kwargs):
        await self._cmd(self.api.set_prop, "FindRobot", 1, 1)

    async def async_set_fan_speed(self, fan_speed, **kwargs):
        if fan_speed not in SUCTION_LEVELS:
            return
        cur = (self.coordinator.data or {}).get("VacWateState")
        await self._cmd(self.api.set_prop, "VacWateState", pack_vws(cur, suction=fan_speed), None, False)
