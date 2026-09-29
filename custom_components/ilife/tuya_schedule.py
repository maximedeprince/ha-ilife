"""Cleaning schedules on ILIFE Clean laser models (worked out on the A30 Pro).

Seven string DPs, `Schedule1`..`Schedule7`, are seven schedule *slots* (not
weekdays: one schedule can cover several days). Each holds base64 of 32 bytes:

    0   hour            1   minute
    2   06 in every capture; left as found
    5   weekday mask, bit 0 = Monday .. bit 6 = Sunday
    6   01 enabled, 00 disabled
    7   program; differs from CleanSettings' numbering and is not fully mapped
        (02 was "standard" on a room schedule), so it is left as found
    24  cycles
    25-28   big-endian room mask, bit N = room id N (as in CleanPartitionData)
    31  02 whole home, 01 selected rooms

An empty slot is 16 bytes: zeros, then 01.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable

SCHEDULE_CODES = tuple(f"Schedule{n}" for n in range(1, 8))
EMPTY_SLOT = base64.b64encode(bytes(15) + b"\x01").decode()
LENGTH = 32
TYPE_ROOMS, TYPE_GLOBAL = 1, 2
MAX_CYCLES = 3


def _decode(value: object) -> bytearray | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = bytearray(base64.b64decode(value))
    except binascii.Error:
        return None
    return raw if len(raw) >= LENGTH and any(raw[:31]) else None


def parse_schedule(value: object) -> dict | None:
    """The schedule in one slot, or None for an empty or unreadable one."""
    raw = _decode(value)
    if raw is None:
        return None
    mask = int.from_bytes(raw[25:29], "big")
    return {
        "time": f"{raw[0]:02d}:{raw[1]:02d}",
        "days": [day for day in range(7) if raw[5] >> day & 1],
        "enabled": bool(raw[6]),
        "type": "rooms" if raw[31] == TYPE_ROOMS else "global",
        "rooms": [room for room in range(32) if mask >> room & 1],
        "cycles": raw[24] or 1,
        "program": raw[7],
    }


def build_schedule(
    existing: object = None,
    *,
    time: str | None = None,
    days: Iterable[int] | None = None,
    enabled: bool | None = None,
    rooms: Iterable[int] | None = None,
    cycles: int | None = None,
) -> str:
    """A slot value with the given fields changed; everything else is kept as the
    app wrote it. `rooms=[]` makes it a whole-home schedule, a non-empty list a
    room schedule. A new slot starts as the app's own: 06, standard program."""
    raw = _decode(existing)
    if raw is None:
        raw = bytearray(LENGTH)
        raw[2], raw[6], raw[7], raw[24], raw[31] = 0x06, 1, 0x02, 1, TYPE_GLOBAL
    else:
        raw = raw[:LENGTH]
    if time is not None:
        hour, minute = (int(part) for part in str(time).split(":")[:2])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"invalid time {time!r}")
        raw[0], raw[1] = hour, minute
    if days is not None:
        mask = 0
        for day in days:
            if not 0 <= day < 7:
                raise ValueError(f"invalid weekday {day}")
            mask |= 1 << day
        if not mask:
            raise ValueError("a schedule needs at least one day")
        raw[5] = mask
    if enabled is not None:
        raw[6] = 1 if enabled else 0
    if rooms is not None:
        mask = 0
        for room in rooms:
            if not 0 <= room < 32:
                raise ValueError(f"room id {room} is out of range")
            mask |= 1 << room
        raw[25:29] = mask.to_bytes(4, "big")
        raw[31] = TYPE_ROOMS if mask else TYPE_GLOBAL
    if cycles is not None:
        if not 1 <= cycles <= MAX_CYCLES:
            raise ValueError(f"cycles must be between 1 and {MAX_CYCLES}")
        raw[24] = cycles
    return base64.b64encode(bytes(raw)).decode()


def parse_schedules(properties: dict) -> dict[int, dict]:
    """{slot number (1-7): schedule} for every non-empty slot."""
    out = {}
    for slot, code in enumerate(SCHEDULE_CODES, start=1):
        schedule = parse_schedule(properties.get(code))
        if schedule is not None:
            out[slot] = schedule
    return out
