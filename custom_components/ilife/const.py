"""Constants for the ILIFE integration."""
DOMAIN = "ilife"
CONF_BRAND = "brand"   # config-entry key: which whitelabel profile (see brands.py)

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
