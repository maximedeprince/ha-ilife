"""Generic entities for ILIFE Clean (Tuya) DP codes we don't have a dedicated mapping
for. The device's live /specifications response is the source of truth for what it
actually supports — nothing here is a guessed/invented DP code or command."""
from __future__ import annotations

import json

from .const import (
    TUYA_DP_MODE,
    TUYA_DP_PAUSE,
    TUYA_DP_POWER_GO,
    TUYA_DP_RETURN_HOME,
    TUYA_DP_SWITCH,
    TUYA_KNOWN_DP_CODES,
)


def parse_functions(spec: dict) -> dict[str, dict]:
    """{code: {"type": str, "values": dict}} from a device's /specifications result."""
    out = {}
    for f in (spec or {}).get("functions") or []:
        code = f.get("code")
        if not code:
            continue
        try:
            values = json.loads(f.get("values") or "{}")
        except (TypeError, ValueError):
            values = {}
        out[code] = {"type": f.get("type"), "values": values}
    return out


def unknown_functions(spec: dict, status: dict, dp_type: str) -> dict[str, dict]:
    """Functions of the given Tuya type (e.g. "Boolean", "Enum") that aren't already
    covered by a dedicated entity, restricted to DPs the device actually reports in its
    status (so we know it's really present on this unit, not just theoretically supported
    by the product category)."""
    functions = parse_functions(spec)
    return {
        code: meta for code, meta in functions.items()
        if meta.get("type") == dp_type and code not in TUYA_KNOWN_DP_CODES and code in status
    }


def unknown_status_values(spec: dict, status: dict) -> list[str]:
    """DP codes actually reported in status that we don't have a dedicated entity for and
    that aren't already exposed as a switch/select (Boolean/Enum functions) — surfaced as
    read-only generic sensors instead of being silently dropped."""
    functions = parse_functions(spec)
    out = []
    for code in status:
        if code in TUYA_KNOWN_DP_CODES:
            continue
        dp_type = (functions.get(code) or {}).get("type")
        if dp_type in ("Boolean", "Enum"):
            continue  # handled by switch.py / select.py
        out.append(code)
    return out


def status_types(spec: dict) -> dict[str, str]:
    """{code: Tuya type} for every DP the device reports in its status block. Unlike
    parse_functions() this also covers read-only DPs (Raw blobs, Bitmaps, counters),
    which is what we need to know how to present a generic sensor."""
    return {
        s["code"]: s.get("type")
        for s in (spec or {}).get("status") or []
        if s.get("code")
    }


# --------------------------------------------------------------------------- #
#  Turning a device's spec into commands
# --------------------------------------------------------------------------- #
def range_values(functions: dict, code: str) -> list:
    """Legal values of an Enum DP, or [] for any other type."""
    return ((functions.get(code) or {}).get("values") or {}).get("range") or []


def match_value(range_list, *candidates):
    """First entry of `range_list` matching one of `candidates`, case-insensitively."""
    lower = {str(v).lower(): v for v in range_list}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def enum_command(functions: dict, code: str, *candidates):
    """(code, value) to drive an Enum DP, or None if this device can't take it."""
    value = match_value(range_values(functions, code), *candidates)
    return None if value is None else (code, value)


def command_for(functions: dict, code: str, *candidates, boolean: bool = True):
    """(code, value) to drive `code` however *this* device actually declares it.

    The same DP is a Boolean on one model and an Enum on the next: `power_go` is a
    plain Boolean on the A30 Pro and the T20s, and an Enum ("start"/"pause"/"stop")
    on other builds. So the type has to be read from the device's own live
    /specifications, never assumed.

    Assuming the Enum shape is exactly what broke start and stop (#23, #24): with a
    Boolean `power_go` the enum match found nothing, the code fell through to
    `switch` — a DP neither model advertises — and Tuya answered 2008 "command or
    value not support" while the working command sat one line away.
    """
    if code not in functions:
        return None
    if range_values(functions, code):
        return enum_command(functions, code, *candidates)
    return (code, boolean)


# --------------------------------------------------------------------------- #
#  Turning an action into the instruction a device obeys
# --------------------------------------------------------------------------- #
# Mode values that send the robot to its dock rather than cleaning (the V20 says
# "goto_charge" where the A30 Pro and the T20s say "chargego").
RETURN_MODES = ("chargego", "charge_go", "back_charge", "goto_charge")


def merge_commands(*pairs) -> list[dict]:
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


def run_flag(functions: dict):
    """(code, value) that sets the robot going, whichever shape its run DP has."""
    return (command_for(functions, TUYA_DP_POWER_GO, "start", "smart", "clean",
                        boolean=True)
            or command_for(functions, TUYA_DP_SWITCH, "start", "smart", "clean",
                           boolean=True))


def unpause_flag(functions: dict):
    """(code, value) that clears the pause flag, or None on a model without one."""
    return command_for(functions, TUYA_DP_PAUSE, "resume", "continue", boolean=False)


def return_commands(functions: dict) -> list[dict]:
    """Send the robot to its dock: the dedicated DP if it has one, else the mode."""
    return merge_commands(
        command_for(functions, TUYA_DP_RETURN_HOME, "chargego", "charge", boolean=True)
        or enum_command(functions, TUYA_DP_MODE, *RETURN_MODES))


def mode_commands(functions: dict, option) -> list[dict]:
    """What choosing `option` in the Cleaning mode select sends (#23).

    Writing `mode` is the command on these robots — there is no separate "go" for a
    zone or a spot clean. But the A30 Pro ignores a mode written on its own, as it
    ignores the run flag on its own (#3, #23, #24), and obeys the instruction Start
    sends: mode, run flag and pause flag together. So a cleaning mode goes out
    exactly like Start, with the chosen mode in place of the full clean, and the
    dock value goes out like Return to base. A model without a run flag still gets
    the bare mode, which is all it has.
    """
    mode = enum_command(functions, TUYA_DP_MODE, str(option).lower())
    if mode is None:
        return []
    if str(mode[1]).lower() in RETURN_MODES:
        return return_commands(functions) or merge_commands(mode)
    return merge_commands(mode, run_flag(functions), unpause_flag(functions))
