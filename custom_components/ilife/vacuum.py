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
    TUYA_DP_MODE,
    TUYA_DP_PAUSE,
    TUYA_DP_POWER_GO,
    TUYA_DP_RETURN_HOME,
    TUYA_DP_STATUS,
    TUYA_DP_SUCTION,
    TUYA_DP_SWITCH,
    TUYA_DP_TOTAL_CLEAN_AREA,
    TUYA_DP_TOTAL_CLEAN_COUNT,
    TUYA_DP_TOTAL_CLEAN_TIME,
    TUYA_MODE_FULL_CLEAN,
    TUYA_STATUS_CLEANING,
    TUYA_STATUS_DOCKED,
    TUYA_STATUS_IDLE,
    TUYA_STATUS_PAUSED,
    TUYA_STATUS_RETURNING,
    pack_vws,
    suction_label,
)
from .entity import ILifeEntity
from .tuya_api import TuyaError, TuyaOfflineError
from .tuya_dynamic import command_for, enum_command, range_values
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


def _commands(*pairs):
    """The (code, value) pairs that this device supports, as one Tuya command list.

    Tuya applies a command list in order, in a single call, which is what lets one
    action write several data points as a single instruction to the robot. Pairs that
    came back None (a data point this model does not have) are simply dropped, so an
    action degrades to whatever the device can actually do rather than failing.

    Pass the pair that carries the action's meaning first: where two of them resolve to
    the same data point, the first wins. On a model whose `power_go` is an Enum, "pause"
    and "stop" are both values of it, and sending both would tell the robot two
    different things in one breath.
    """
    out = {}
    for pair in pairs:
        if pair is not None:
            code, value = pair
            out.setdefault(code, value)
    return [{"code": code, "value": value} for code, value in out.items()]


class TuyaVacuum(TuyaEntity, StateVacuumEntity):
    """ILIFE Clean (Tuya) vacuum. Features/commands are derived from the device's own
    live /specifications response — nothing here assumes a DP the device didn't advertise."""

    _attr_name = None

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.device_id}_vacuum"
        functions = coordinator.spec_functions

        # Each action is resolved once, against this device's own spec, into the list of
        # data points to write for it. A feature is advertised only when there is a
        # command to back it.
        #
        # The run flag and the pause flag are written **together**, in one Tuya command.
        # That is not belt and braces: ILIFE firmware ignores `power_go` on its own
        # (v0.6.0 sent exactly that, Tuya accepted it, and three vacuums did nothing —
        # #3, #23, #24), while tuya-local drives the same robots locally by always
        # writing both flags in the same frame, and works. Home Assistant's own Tuya
        # integration sends `power_go` alone, so it has the same blind spot.
        run = command_for(functions, TUYA_DP_POWER_GO, "start", "smart", "clean",
                          boolean=True) \
            or command_for(functions, TUYA_DP_SWITCH, "start", "smart", "clean",
                           boolean=True)
        halt = command_for(functions, TUYA_DP_POWER_GO, "stop", "standby", "idle",
                           boolean=False) \
            or command_for(functions, TUYA_DP_SWITCH, "stop", "standby", "idle",
                           boolean=False)
        # Never derive pause from a Boolean power_go: false there means stop, not pause.
        hold = command_for(functions, TUYA_DP_PAUSE, "pause", boolean=True) \
            or enum_command(functions, TUYA_DP_POWER_GO, "pause")
        unhold = command_for(functions, TUYA_DP_PAUSE, "resume", "continue",
                             boolean=False)
        full_clean = enum_command(functions, TUYA_DP_MODE, *TUYA_MODE_FULL_CLEAN)

        # Each list is mode first, then the run flag, then the pause flag.
        #
        # START means "clean the place", so it asks for the full-clean mode as well:
        # writing `mode` is what demonstrably starts these robots (#24), and a device
        # left in `part`/`zone` would otherwise answer Start with a spot clean. The
        # Cleaning mode select remains the way to ask for those.
        #
        # Each action only exists if something can really perform it. In particular
        # PAUSE requires a genuine pause data point: a Boolean `power_go` set to false
        # means stop, and offering that as pause would quietly lose the user's place.
        self._cmd_start = _commands(full_clean, run, unhold) if (full_clean or run) else []
        self._cmd_resume = _commands(unhold, run) if (run or unhold) else []
        self._cmd_pause = _commands(hold, halt) if hold else []
        self._cmd_stop = _commands(halt, unhold) if halt else []
        self._cmd_return = _commands(
            command_for(functions, TUYA_DP_RETURN_HOME, "chargego", "charge",
                        boolean=True)
            or enum_command(functions, TUYA_DP_MODE, "chargego", "charge_go",
                            "back_charge"))
        self._cmd_locate = _commands(
            command_for(functions, TUYA_DP_LOCATE, "seek", boolean=True))
        # Suction is the fan speed every ILIFE Clean model advertises; exposing it as
        # the vacuum's own fan_speed is what the Lovelace card (and HA's stock vacuum
        # card) drive, instead of a nameless generic select nothing knows about.
        self._suction_range = [str(v) for v in range_values(functions, TUYA_DP_SUCTION)]

        features = VacuumEntityFeature.STATE
        if self._cmd_start:
            features |= VacuumEntityFeature.START
        if self._cmd_stop:
            features |= VacuumEntityFeature.STOP
        if self._cmd_pause:
            features |= VacuumEntityFeature.PAUSE
        if self._cmd_return:
            features |= VacuumEntityFeature.RETURN_HOME
        if self._cmd_locate:
            features |= VacuumEntityFeature.LOCATE
        if self._suction_range:
            features |= VacuumEntityFeature.FAN_SPEED
            self._attr_fan_speed_list = self._suction_range
        self._attr_supported_features = features

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
            if s in TUYA_STATUS_CLEANING:
                return VacuumActivity.CLEANING
            # An unrecognised status used to mean "cleaning", which reported a docked
            # vacuum as cleaning for good. Charging is the value models rename most, so
            # try that, then believe the run flags over a guess.
            if "charg" in s or "dock" in s:
                return VacuumActivity.DOCKED
        if data.get(TUYA_DP_PAUSE) is True:
            return VacuumActivity.PAUSED
        if data.get(TUYA_DP_POWER_GO) is True or data.get(TUYA_DP_SWITCH) is True:
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self):
        v = (self.coordinator.data or {}).get(TUYA_DP_SUCTION)
        return None if v is None else str(v)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        attrs = {}
        fault = data.get(TUYA_DP_FAULT)
        if fault:
            attrs["fault"] = fault
        for key, code in (
            ("total_clean_count", TUYA_DP_TOTAL_CLEAN_COUNT),
            ("total_clean_area", TUYA_DP_TOTAL_CLEAN_AREA),
            ("total_clean_time", TUYA_DP_TOTAL_CLEAN_TIME),
        ):
            value = data.get(code)
            if value is not None:
                attrs[key] = value
        return attrs

    async def _send(self, code, value):
        await self._send_many([{"code": code, "value": value}])

    async def _send_many(self, commands):
        try:
            await self.hass.async_add_executor_job(self.api.send_many, commands)
        except TuyaOfflineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_offline"
            ) from err
        except TuyaError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def _run(self, commands, action):
        if not commands:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_unsupported",
                translation_placeholders={
                    "action": action,
                    "device": self.api.device.get("product_name") or "this vacuum",
                },
            )
        await self._send_many(commands)

    @property
    def _is_paused(self):
        data = self.coordinator.data or {}
        if data.get(TUYA_DP_PAUSE) is True:
            return True
        return str(data.get(TUYA_DP_STATUS) or "").lower() in TUYA_STATUS_PAUSED

    async def async_start(self):
        # Resuming a paused clean must not restart it from scratch, which asking for
        # the full-clean mode again would do.
        if self._is_paused and self._cmd_resume:
            await self._send_many(self._cmd_resume)
            return
        await self._run(self._cmd_start, "start")

    async def async_pause(self):
        await self._run(self._cmd_pause, "pause")

    async def async_stop(self, **kwargs):
        await self._run(self._cmd_stop, "stop")

    async def async_return_to_base(self, **kwargs):
        await self._run(self._cmd_return, "return to base")

    async def async_locate(self, **kwargs):
        await self._run(self._cmd_locate, "locate")

    async def async_set_fan_speed(self, fan_speed, **kwargs):
        if fan_speed not in self._suction_range:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="fan_speed_unsupported",
                translation_placeholders={
                    "fan_speed": str(fan_speed),
                    "options": ", ".join(self._suction_range) or "none",
                },
            )
        await self._send(TUYA_DP_SUCTION, fan_speed)
