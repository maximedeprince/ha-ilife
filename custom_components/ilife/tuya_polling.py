"""How often the ILIFE Clean backend calls Tuya's cloud (#32).

Every call counts against the user's own Tuya Cloud Project. The free Trial Edition
allows about 26,000 calls a month and does not let them run over: once they are spent,
Tuya suspends the project until the quota refreshes (error 28841004), and the vacuum is
gone from Home Assistant for the rest of the month. Reading the status every 30 seconds,
with the properties, the stored-map list and the map on top, came to about 4,000 calls a
day for one vacuum sitting on its dock: the month's allowance in about a week.

So the status is read every ACTIVE_INTERVAL only while the vacuum is cleaning or on its
way back, or was just sent a command, and every IDLE_INTERVAL (per vacuum on the
account) otherwise; what changes rarely is read rarely. A vacuum that cleans for an hour
a day now costs under 500 calls a day, about 14,000 a month.

No Home Assistant imports: the tests load this module on its own.
"""
from __future__ import annotations

from .const import (
    TUYA_DP_PAUSE,
    TUYA_DP_POWER_GO,
    TUYA_DP_STATUS,
    TUYA_DP_SWITCH,
    TUYA_STATUS_CLEANING,
    TUYA_STATUS_DOCKED,
    TUYA_STATUS_IDLE,
    TUYA_STATUS_PAUSED,
    TUYA_STATUS_RETURNING,
)

# The free Trial Edition's monthly allowance, as Tuya's pricing page puts it.
TRIAL_CALLS_PER_MONTH = 26_000

# Status reads, in seconds.
ACTIVE_INTERVAL = 30
IDLE_INTERVAL = 300
# After a command, so that the run it started shows up without waiting for an idle poll.
COMMAND_FOLLOW_UP = 180
# Once the month's quota is spent, nothing gets through until it refreshes: one try an
# hour is enough to notice when it has.
QUOTA_RETRY_INTERVAL = 3600

# Room names and schedules: they change when they are edited in the app, and are read
# again right after this integration writes them.
PROPERTIES_INTERVAL = 3600

# The stored-map list (cleaning history): a new entry only appears when a run ends, so
# it is read often for a while after one, and rarely otherwise.
HISTORY_INTERVAL = 6 * 3600
HISTORY_AFTER_RUN = 900
HISTORY_AFTER_RUN_INTERVAL = 60

# The map camera. The newest stored map only changes when a run ends, and no ILIFE model
# has filled the live map endpoint so far: during a run it is asked a few times, then
# left alone until the next run, unless it once answered with a map.
MAP_IDLE_INTERVAL = 6 * 3600
MAP_ACTIVE_INTERVAL = 30  # during a run: once the probes are done, a re-render, no call
MAP_AFTER_RUN = 600
MAP_AFTER_RUN_INTERVAL = 120
MAP_REALTIME_TRIES = 3
# A map that keeps failing (no Sweeping Robot service, a model without maps) is tried
# less and less often, and at the idle pace in the end.
MAP_RETRY_BACKOFF = (30, 60, 300, 900, 3600, MAP_IDLE_INTERVAL)


def busy(status: dict) -> bool:
    """Whether the vacuum is cleaning or heading back to its dock: the only times its
    status changes from one minute to the next. Docked, idle, paused or in error, it
    waits for someone, and a command makes the coordinator follow it closely anyway."""
    value = status.get(TUYA_DP_STATUS)
    if isinstance(value, str):
        s = value.lower()
        if s in TUYA_STATUS_CLEANING or s in TUYA_STATUS_RETURNING:
            return True
        if (s in TUYA_STATUS_DOCKED or s in TUYA_STATUS_IDLE or s in TUYA_STATUS_PAUSED
                or "charg" in s or "dock" in s):
            return False
    if status.get(TUYA_DP_PAUSE) is True:
        return False
    return status.get(TUYA_DP_POWER_GO) is True or status.get(TUYA_DP_SWITCH) is True


def poll_interval(is_busy: bool, vacuums: int, now: float, follow_up_until: float) -> int:
    """Seconds until the next status read. The idle interval grows with the number of
    vacuums, which share one Cloud Project and so one quota."""
    if is_busy or now < follow_up_until:
        return ACTIVE_INTERVAL
    return IDLE_INTERVAL * max(1, vacuums)


def calls_per_day(busy_hours: float = 1.0, vacuums: int = 1) -> float:
    """Expected calls a day for one vacuum on an account of `vacuums`, busy (cleaning
    or returning) `busy_hours` a day in one run. The README quotes this figure, and the
    tests keep it under the Trial Edition's allowance."""
    status = (busy_hours * 3600 / ACTIVE_INTERVAL
              + (24 - busy_hours) * 3600 / (IDLE_INTERVAL * max(1, vacuums)))
    tokens = 12 / max(1, vacuums)  # one 2-hour token, shared by the account
    properties = 24 * 3600 / PROPERTIES_INTERVAL
    history = (24 * 3600 / HISTORY_INTERVAL
               + HISTORY_AFTER_RUN / (IDLE_INTERVAL * max(1, vacuums)) + 1)
    camera = (24 * 3600 / MAP_IDLE_INTERVAL
              + 2 * MAP_REALTIME_TRIES  # live-map probes and the requests before them
              + MAP_AFTER_RUN / MAP_AFTER_RUN_INTERVAL + 1)
    return status + tokens + properties + history + camera
