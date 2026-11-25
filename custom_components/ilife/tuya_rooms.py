"""Rooms, room cleaning and cleaning programs on ILIFE Clean laser models (A30 Pro).

Neither lives in the standard instruction set the REST commands use. They are
product DPs, reachable through the thing-model API
(`/v2.0/cloud/thing/{id}/shadow/properties`):

    MapRoomInfo1..3, map_room_info_4..5   string, base64 of
        "<map id>,<map name>,<room id>,<room name>,<room id>,<room name>,..."
        e.g. "1762447000,mapName,4,Office,0,Living room,2,Hall,..."
        Room ids are the ones in the map grid (`tuya_map`). Each DP holds one
        saved map (up to five multi-floor / remapped slots, as on ILIFEHOME SLAM
        models). Joining them blindly glued the next map's leading Unix timestamp
        onto the previous map's last room name and dropped the newer map (#44).
        A DP that does not start with a map header is still appended to the
        previous record, in case a very long room list ever wraps into the next DP.

    CleanSettings                         string, base64: two epoch-second stamps
        (4 bytes each — the first is the active map's timestamp, matching
        MapRoomInfo's leading map id), the room count, the active program
        (01 standard, 02 plan 1, 03 plan 2), then 3 bytes per room for plan 1
        and again for plan 2 (room id, order | ..., cycles | ...). The app picks
        a program by writing it back with only the program byte changed, then
        starts a normal clean.

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


def _starts_new_map(chunk: str, previous: str | None) -> bool:
    """Whether `chunk` begins a separate saved-map record rather than continuing
    `previous`. A real map header starts with a Unix timestamp (> MAX_ROOM_ID),
    followed by the map name."""
    if previous is None:
        return True
    parts = chunk.split(",")
    if len(parts) < 2 or not parts[0].strip().isdigit():
        return False
    if int(parts[0].strip()) > MAX_ROOM_ID:
        return True
    prev_parts = previous.split(",")
    return (
        not previous.endswith(",")
        and len(prev_parts) >= 2
        and len(prev_parts) % 2 == 0
        and len(parts) >= 4
        and parts[2].strip().isdigit()
    )


def parse_room_maps(properties: Mapping[str, object]) -> list[dict[str, object]]:
    """Every saved map found in the MapRoomInfo DPs: [{map_id, name, rooms}, ...]."""
    records: list[str] = []
    for code in ROOM_INFO_CODES:
        value = properties.get(code)
        if not isinstance(value, str) or not value:
            continue
        try:
            chunk = base64.b64decode(value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        if not chunk:
            continue
        if _starts_new_map(chunk, records[-1] if records else None):
            records.append(chunk)
        else:
            records[-1] += chunk

    maps: list[dict[str, object]] = []
    for text in records:
        parts = text.split(",")
        if len(parts) < 2:
            continue
        head = parts[0].strip()
        map_id = int(head) if head.isdigit() else None
        map_name = parts[1].strip()
        fields = parts[2:]
        rooms: dict[int, str] = {}
        for room_id, name in zip(fields[0::2], fields[1::2]):
            rid = room_id.strip()
            label = name.strip()
            if rid.isdigit() and label:
                num = int(rid)
                if 0 <= num <= MAX_ROOM_ID:
                    rooms[num] = label
        maps.append({"map_id": map_id, "name": map_name, "rooms": rooms})
    return maps


def parse_room_names(
    properties: Mapping[str, object],
    *,
    map_room_ids: Iterable[int] | None = None,
) -> dict[int, str]:
    """{room id: name} for the active map from the MapRoomInfo DPs.

    When several saved maps are present, pick the active one in order:
      1. the slot whose map id matches CleanSettings' leading map timestamp
         (unless its room ids contradict `map_room_ids` while another slot fits);
      2. the slot whose room ids best fit `map_room_ids` (no phantom rooms,
         largest overlap, newest map id as tie-breaker);
      3. the slot with the newest map id.
    """
    maps = parse_room_maps(properties)
    if not maps:
        return {}
    if len(maps) == 1:
        return dict(maps[0]["rooms"])

    target = set(map_room_ids) if map_room_ids else None
    settings_map = active_map_id(properties.get(CLEAN_SETTINGS_CODE))
    by_settings = (
        next((m for m in maps if m["map_id"] == settings_map), None)
        if settings_map is not None
        else None
    )
    if by_settings is not None:
        if target is None or set(by_settings["rooms"]) <= target:
            return dict(by_settings["rooms"])
        if not any(m["rooms"] and set(m["rooms"]) <= target for m in maps):
            return dict(by_settings["rooms"])

    if target is not None:
        candidates = [m for m in maps if m["rooms"]]
        if candidates:
            def _score(entry: dict[str, object]) -> tuple[int, int, int, int]:
                rids = set(entry["rooms"])
                return (
                    1 if rids <= target else 0,
                    len(rids & target),
                    -len(rids ^ target),
                    entry["map_id"] or 0,
                )

            return dict(max(candidates, key=_score)["rooms"])

    best = max(
        enumerate(maps),
        key=lambda item: (item[1]["map_id"] or 0, item[0]),
    )[1]
    return dict(best["rooms"])


def _settings_bytes(value: object) -> bytearray | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = bytearray(base64.b64decode(value))
    except binascii.Error:
        return None
    return raw if len(raw) > _PROGRAM_OFFSET else None


def active_map_id(settings: object) -> int | None:
    """The active map timestamp in the first 4 bytes of CleanSettings, or None."""
    raw = _settings_bytes(settings)
    if raw is None:
        return None
    map_id = int.from_bytes(raw[:4], "big")
    return map_id or None


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
