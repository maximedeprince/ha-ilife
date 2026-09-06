"""ILIFE vacuum entity (ILIFEHOME) and ILIFE Clean (Tuya) vacuum entity."""
from __future__ import annotations

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

from .api import ILifeError, ILifeOfflineError
from .const import (
    BACKEND_ILIFE_CLEAN,
    CLEANING_MODES,
    DOCKED_MODES,
    DOMAIN,
    PAUSED_MODES,
    RETURNING_MODES,
    SUCTION_LEVELS,
    TUYA_DP_FAULT,
    TUYA_DP_LOCATE,
    TUYA_DP_PAUSE,
    TUYA_DP_POWER_GO,
    TUYA_DP_RETURN_HOME,
    TUYA_DP_STATUS,
    TUYA_DP_SWITCH,
    TUYA_STATUS_DOCKED,
    TUYA_STATUS_IDLE,
    TUYA_STATUS_PAUSED,
    TUYA_STATUS_RETURNING,
    pack_vws,
    suction_label,
)
from .entity import ILifeEntity
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_entity import TuyaEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    if data.get("backend") == BACKEND_ILIFE_CLEAN:
        async_add_entities(TuyaVacuum(c) for c in data["coordinators"].values())
    else:
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


def _range_values(spec_functions, code):
    return ((spec_functions.get(code) or {}).get("values") or {}).get("range") or []


def _match(range_list, *candidates):
    """First entry of `range_list` matching one of `candidates`, case-insensitively."""
    lower = {str(v).lower(): v for v in range_list}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


class TuyaVacuum(TuyaEntity, StateVacuumEntity):
    """ILIFE Clean (Tuya) vacuum. Features/commands are derived from the device's own
    live /specifications response — nothing here assumes a DP the device didn't advertise."""

    _attr_name = None

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_vacuum"
        functions = coordinator.spec_functions
        features = VacuumEntityFeature.STATE
        if TUYA_DP_SWITCH in functions or TUYA_DP_POWER_GO in functions:
            features |= VacuumEntityFeature.START | VacuumEntityFeature.STOP
        if TUYA_DP_PAUSE in functions or TUYA_DP_POWER_GO in functions:
            features |= VacuumEntityFeature.PAUSE
        if TUYA_DP_RETURN_HOME in functions:
            features |= VacuumEntityFeature.RETURN_HOME
        if TUYA_DP_LOCATE in functions:
            features |= VacuumEntityFeature.LOCATE
        self._attr_supported_features = features
        self._power_go_range = _range_values(functions, TUYA_DP_POWER_GO)

    @property
    def activity(self):
        data = self.coordinator.data or {}
        status = data.get(TUYA_DP_STATUS)
        if isinstance(status, str):
            s = status.lower()
            if s in TUYA_STATUS_DOCKED:
                return VacuumActivity.DOCKED
            if s in TUYA_STATUS_RETURNING:
                return VacuumActivity.RETURNING
            if s in TUYA_STATUS_PAUSED:
                return VacuumActivity.PAUSED
            if s in TUYA_STATUS_IDLE:
                return VacuumActivity.IDLE
            return VacuumActivity.CLEANING
        if data.get(TUYA_DP_PAUSE) is True:
            return VacuumActivity.PAUSED
        if data.get(TUYA_DP_SWITCH) is True:
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def extra_state_attributes(self):
        fault = (self.coordinator.data or {}).get(TUYA_DP_FAULT)
        return {"fault": fault} if fault else {}

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

    async def async_start(self):
        start = _match(self._power_go_range, "start", "smart", "clean")
        if start is not None:
            await self._send(TUYA_DP_POWER_GO, start)
        else:
            await self._send(TUYA_DP_SWITCH, True)

    async def async_pause(self):
        pause = _match(self._power_go_range, "pause")
        if pause is not None:
            await self._send(TUYA_DP_POWER_GO, pause)
        else:
            await self._send(TUYA_DP_PAUSE, True)

    async def async_stop(self, **kwargs):
        stop = _match(self._power_go_range, "stop")
        if stop is not None:
            await self._send(TUYA_DP_POWER_GO, stop)
        else:
            await self._send(TUYA_DP_SWITCH, False)

    async def async_return_to_base(self, **kwargs):
        await self._send(TUYA_DP_RETURN_HOME, True)

    async def async_locate(self, **kwargs):
        await self._send(TUYA_DP_LOCATE, True)
