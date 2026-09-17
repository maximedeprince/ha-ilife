"""Generic entities for ILIFE Clean (Tuya) DP codes we don't have a dedicated mapping
for. The device's live /specifications response is the source of truth for what it
actually supports — nothing here is a guessed/invented DP code or command."""
from __future__ import annotations

import json

from .const import TUYA_KNOWN_DP_CODES


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
