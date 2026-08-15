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
