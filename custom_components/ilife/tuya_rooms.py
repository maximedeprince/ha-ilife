"""Rooms, room cleaning and cleaning programs on ILIFE Clean laser models (A30 Pro).

Neither lives in the standard instruction set the REST commands use. They are
product DPs, reachable through the thing-model API
(`/v2.0/cloud/thing/{id}/shadow/properties`):

    MapRoomInfo1..3, map_room_info_4..5   string, base64 of
        "<map id>,<map name>,<room id>,<room name>,<room id>,<room name>,..."
        e.g. "1762447000,mapName,4,Office,0,Living room,2,Hall,..."
        Room ids are the ones in the map grid (`tuya_map`). Longer lists continue
        in the next DP, so the chunks are joined before splitting.

    CleanSettings                         string, base64: two epoch-second stamps
        (4 bytes each), the room count, the active program (01 standard, 02 plan 1,
        03 plan 2), then 3 bytes per room for plan 1 and again for plan 2
        (room id, order | ..., cycles | ...). The app picks a program by writing
        it back with only the program byte changed, then starts a normal clean.

    CleanPartitionData                    string, base64 of 6 bytes
        01, passes (1 or 2: twice runs a second, crosswise pass), then a
        big-endian 32-bit mask with bit N set for room id N. Writing it starts
        the room clean on its own; the robot echoes it with the pass in progress.
        Idle it reads 00 00 00 00 00 00.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping

ROOM_INFO_CODES = (
    "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3", "map_room_info_4", "map_room_info_5",
)
CLEAN_ROOMS_CODE = "CleanPartitionData"
CLEAN_SETTINGS_CODE = "CleanSettings"
PROGRAMS = {1: "standard", 2: "plan_1", 3: "plan_2"}
_PROGRAM_OFFSET = 9
MAX_ROOM_ID = 31
MAX_PASSES = 2


def parse_room_names(properties: Mapping[str, object]) -> dict[int, str]:
    """{room id: name} from the MapRoomInfo DPs; empty when the robot has none."""
    text = ""
    for code in ROOM_INFO_CODES:
        value = properties.get(code)
        if isinstance(value, str) and value:
            try:
                text += base64.b64decode(value).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return {}
    fields = text.split(",")[2:]  # past the map id and the map name
    names = {}
    for room_id, name in zip(fields[0::2], fields[1::2]):
        if room_id.strip().isdigit() and name.strip():
            names[int(room_id)] = name.strip()
    return names


def _settings_bytes(value: object) -> bytearray | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = bytearray(base64.b64decode(value))
    except binascii.Error:
        return None
    return raw if len(raw) > _PROGRAM_OFFSET else None


def active_program(settings: object) -> str | None:
    """The program ("standard", "plan_1", "plan_2") a CleanSettings value selects."""
    raw = _settings_bytes(settings)
    return PROGRAMS.get(raw[_PROGRAM_OFFSET]) if raw else None


def with_program(settings: object, program: str) -> str:
    """`settings` with only the program byte changed, as the app writes it."""
    raw = _settings_bytes(settings)
    if raw is None:
        raise ValueError("the vacuum has not reported its cleaning settings")
    codes = {name: code for code, name in PROGRAMS.items()}
    if program not in codes:
        raise ValueError(f"unknown program {program!r}")
    raw[_PROGRAM_OFFSET] = codes[program]
    return base64.b64encode(bytes(raw)).decode()


def clean_rooms_value(room_ids: Iterable[int], passes: int = 1) -> str:
    """The CleanPartitionData value that cleans `room_ids`, each `passes` times."""
    if not 1 <= passes <= MAX_PASSES:
        raise ValueError(f"passes must be between 1 and {MAX_PASSES}")
    mask = 0
    for room_id in room_ids:
        if not 0 <= room_id <= MAX_ROOM_ID:
            raise ValueError(f"room id {room_id} is out of range")
        mask |= 1 << room_id
    if not mask:
        raise ValueError("no room selected")
    return base64.b64encode(bytes([1, passes]) + mask.to_bytes(4, "big")).decode()
