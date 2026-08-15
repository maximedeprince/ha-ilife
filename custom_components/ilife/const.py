"""Constants for the ILIFE integration."""
DOMAIN = "ilife"

# Which cloud backend a config entry talks to.
BACKEND_ILIFEHOME = "ilifehome"
BACKEND_ILIFE_CLEAN = "ilife_clean"

CONF_BACKEND = "backend"
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_UID = "uid"

# WorkMode groups used to derive the vacuum activity
CLEANING_MODES = {3, 4, 5, 6, 13, 10}
DOCKED_MODES = {9, 11, 16}
RETURNING_MODES = {8}
PAUSED_MODES = {12}

# CleanDirection (remote-control mode)
DIRECTIONS = {"forward": 1, "backward": 2, "left": 3, "right": 4, "pause": 5}

# VacWateState : one packed byte -> (suction << 4) | water
#   high nibble = suction (1-4), low nibble = water (1-3)
SUCTION_LEVELS = {"Gentle": 1, "Standard": 2, "Strong": 3, "Max": 4}
WATER_LEVELS = {"Gentle": 1, "Standard": 2, "Strong": 3}
_SUCTION_REV = {v: k for k, v in SUCTION_LEVELS.items()}
_WATER_REV = {v: k for k, v in WATER_LEVELS.items()}


def suction_label(vws):
    """Suction label from VacWateState (high nibble)."""
    return _SUCTION_REV.get(((vws or 0) >> 4) & 0xF)


def water_label(vws):
    """Water label from VacWateState (low nibble)."""
    return _WATER_REV.get((vws or 0) & 0xF)


def pack_vws(vws, suction=None, water=None):
    """Recompose VacWateState, preserving the untouched nibble."""
    hi = SUCTION_LEVELS.get(suction, ((vws or 0) >> 4) & 0xF)
    lo = WATER_LEVELS.get(water, (vws or 0) & 0xF)
    return (hi << 4) | lo


# Cleaning mode = WorkMode sent on START (not a persistent property)
CLEAN_MODES = {"S-shape": 6, "Auto": 3}
_CLEANMODE_REV = {v: k for k, v in CLEAN_MODES.items()}
DEFAULT_START_MODE = 6  # S-shape


def clean_mode_label(workmode):
    """Label if the current WorkMode is one of the start modes, else None."""
    return _CLEANMODE_REV.get(workmode)


# Schedules: slot N = weekday N (1=Monday … 7=Sunday, ScheduleWeek)
SCHEDULE_SLOTS = range(1, 8)


def default_schedule(n):
    return {"ScheduleHour": 8, "ScheduleEnd": 0, "ScheduleEnable": 0,
            "ScheduleMode": 3, "ScheduleWeek": n, "ScheduleArea": 0, "ScheduleMinutes": 0}


def modify_schedule(data, n, **changes):
    """Take the current struct of slot N and change only the given fields."""
    cur = (data or {}).get(f"Schedule{n}")
    struct = dict(cur) if isinstance(cur, dict) else default_schedule(n)
    struct.update(changes)
    return struct


# --------------------------------------------------------------------------- #
#  ILIFE Clean (Tuya) backend
# --------------------------------------------------------------------------- #
# DP codes below are Tuya's own standard names for the "sd" (robot vacuum) product
# category, verified against Tuya's official (archived) tuya_v2 Home Assistant
# component (github.com/tuya/tuya-home-assistant) rather than guessed.
TUYA_DP_SWITCH = "switch"
TUYA_DP_POWER_GO = "power_go"
TUYA_DP_STATUS = "status"
TUYA_DP_PAUSE = "pause"
TUYA_DP_RETURN_HOME = "switch_charge"
TUYA_DP_BATTERY = "electricity_left"
TUYA_DP_LOCATE = "seek"
TUYA_DP_CLEAN_AREA = "clean_area"
TUYA_DP_CLEAN_TIME = "clean_time"
TUYA_DP_CLEAN_RECORD = "clean_record"
TUYA_DP_FAULT = "fault"
TUYA_DP_MODE = "mode"

TUYA_STATUS_DOCKED = {"charging", "charge_done", "chargecompleted", "standby_charge"}
TUYA_STATUS_RETURNING = {"goto_charge", "docking", "back_charge"}
TUYA_STATUS_IDLE = {"standby", "sleep"}
TUYA_STATUS_PAUSED = {"pause", "paused"}

# DP codes surfaced through dedicated entities. Any other DP code the device actually
# advertises (in its live /specifications response) gets a *generic* entity instead of
# being silently dropped — see tuya_dynamic.py. Nothing is invented for a DP we can't see.
TUYA_KNOWN_DP_CODES = {
    TUYA_DP_SWITCH, TUYA_DP_POWER_GO, TUYA_DP_STATUS, TUYA_DP_PAUSE, TUYA_DP_RETURN_HOME,
    TUYA_DP_BATTERY, TUYA_DP_LOCATE, TUYA_DP_CLEAN_AREA, TUYA_DP_CLEAN_TIME,
    TUYA_DP_CLEAN_RECORD, TUYA_DP_FAULT, TUYA_DP_MODE,
}
