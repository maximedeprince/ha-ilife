"""Map / command protocol per model family.

Brands (brands.py) say how to reach the cloud; protocols say how a given *model* reports
its map and which map commands it understands. The app picks the screen from its robot
config's `mapType` per productKey (RobotFragment): 1 = gyro / real-time map, 2 = X801,
3 = SLAM "X9" (saved maps + rooms), 4 = W450. The same table is shared by every brand
built on the ILIFE app (ILIFE, ZACO, AVA...), so an ILIFE A10 and an AVA PRO MAX speak
the same protocol.

mapType 3 (SlamX9Protocol) and 4 (W450Protocol) have dedicated implementations; everything else keeps the real-time map
behaviour the integration always had. A device whose productKey is unknown is classified
from the properties it reports.
"""
from __future__ import annotations

import functools

import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .api import ILifeError
from .const import CLEANING_MODES, DOMAIN, PAUSED_MODES

_LOGGER = logging.getLogger(__name__)

# productKey -> (model, factory type, app mapType), from the app's assets
# (over_sea_robot.json, china_robot.json, zaco_robot.json, zhiyi_common.json).
PRODUCTS: dict[str, tuple[str, str, int]] = {
    # SLAM "X9" family (mapType 3)
    "a1v4lbajRA6": ("A10/A10s", "X901", 3),
    "a1xvBzVBKC7": ("L100", "X901", 3),
    "a1LuXMkxW3w": ("A10 White", "X901", 3),
    "a1wzCC1Mr2b": ("ZACO A10", "X901", 3),
    "a1YvZft991f": ("X900", "X901", 3),
    "a1TRUynRz1n": ("X900", "X901", 3),
    "a1qo3jtvcHA": ("X910", "X901", 3),
    "a1AmukCkzWy": ("AVA PRO MAX", "X901", 3),
    # X801 (mapType 2)
    "a1sd2QvPA3Z": ("A9s Pro", "X801", 2),
    "a2n0QiJDnzc": ("ZACO A9s Pro", "X801", 2),
    "a1Di9sgTsqw": ("ZACO A9s Pro White", "X801", 2),
    "a1EBXRXFnvx": ("X801", "X801", 2),
    # W450 (mapType 4)
    "a1en2MuWeRv": ("W400", "W450", 4),
    "a1hTwV7eU4O": ("W450", "W450", 4),
    "a1b25BrPLzw": ("W455", "W450", 4),
    "a1j4VQMYVbz": ("ZACO W450", "W450", 4),
    # gyro / real-time map (mapType 1)
    "a2hZYsA4x5q": ("A80 Plus", "X781", 1),
    "a2gylz5SVpC": ("V80 Pro", "X752", 1),
    "a1xQKOoCYu1": ("V8 Pro/V80 Max", "X758", 1),
    "a1oQ9BL8Lmw": ("V9e", "V500", 1),
    "a2OrF0qnBjz": ("V5x", "X320", 1),
    "a1nLh0u1Go0": ("A80 Pro", "X786", 1),
    "a1ByWiaLJSg": ("A80 Max", "X781", 1),
    "a1FWFHHCMxP": ("A9/A9s", "X800", 1),
    "a1BzC0rwhuf": ("B5 Max", "X350", 1),
    "a1LNWRzDXe7": ("A9/A9s", "X800", 1),
    "a1ZuVe6jF66": ("X420", "X434", 1),
    "a1pOsUna3pk": ("X782", "X781", 1),
    "a1iRIme2Vph": ("A4sPro", "X320", 1),
    "a1kSA2TI54J": ("A8 Max", "X781", 1),
    "a1H3cya3tqZ": ("V3s Max", "X350", 1),
    "a1Y2xcCWkVs": ("ZACO V5x", "X320", 1),
    "a1fa6nwIt4h": ("ZACO V6", "X350", 1),
    "a1IUadB5sjs": ("ZACO V85", "X752", 1),
    "a1id5DRpaIX": ("ZACO A8s", "X752", 1),
    "a1nIeZXrVFg": ("X800", "X800", 1),
    "a1cdxiwZfP9": ("X800", "X800", 1),
    "a1wBeDmFnJu": ("X787", "X787", 1),
    "a1r76ksgLTV": ("V3x", "X320", 1),
    "a1uDIqJ4a8u": ("V3x", "X320", 1),  # the EU V3x, as a live one reports it
    "a1JyoDFAJQT": ("X451", "X434", 1),
    "a1ogWJdRVNo": ("X781", "X781", 1),
}

_SLAM_PROPS = ("SaveMap", "RealMapRoadData")


class MapProtocol:
    """Base: what the camera / vacuum ask of a model family."""

    key = "base"
    rooms = False   # ilife.clean_rooms
    zones = False   # ilife.clean_zone
    goto = False    # ilife.go_to
    commands = False  # any of the above, i.e. vacuum.send_command is meaningful

    def __init__(self, api) -> None:
        self.api = api

    async def async_update(self, hass, state: dict) -> None:
        """Called on every poll with the fresh state (may enrich it in place)."""

    def has_map(self, state: dict) -> bool:
        """Whether there is something to draw (camera availability)."""
        return False

    def render_key(self, state: dict):
        """Changes whenever the map image must be re-rendered."""
        return None

    def render(self, state: dict) -> bytes | None:
        """Map PNG (runs in the executor)."""
        return None

    def camera_attributes(self) -> dict | None:
        return None

    def vacuum_attributes(self) -> dict | None:
        return None

    def require_map(self, capability: str) -> None:
        """Raise when a map command can't run yet (e.g. no saved map)."""

    def diagnostics(self) -> dict:
        """What the protocol made of the map, for the diagnostics file: sizes and
        counts only, never the plan or the room names."""
        return {"key": self.key}


class RealTimeMapProtocol(MapProtocol):
    """Gyro models (mapType 1) and anything without a dedicated protocol:
    RealTimeMap.MapData is a small rolling window of 5-byte cell records, merged here
    into the full current-clean map and injected back into the state. map.render_png
    falls back to the RealMapData_1 live map when there is no RealTimeMap."""

    key = "realtime"

    def __init__(self, api) -> None:
        super().__init__(api)
        self._cells: dict[tuple[int, int], int] = {}
        self._session: Any = None

    async def async_update(self, hass, state: dict) -> None:
        from .map import decode_cells, encode_cells

        rtm = state.get("RealTimeMap") or {}
        if state.get("WorkMode") not in CLEANING_MODES:
            self._session = None  # next clean starts a fresh map
            return
        md = rtm.get("MapData")
        if not md:
            return
        session = state.get("RealTimeMapStart")
        if session != self._session:
            self._session = session
            self._cells = {}
        for x, y, t in decode_cells(md):
            self._cells[(x, y)] = t
        merged = dict(rtm)
        merged["MapData"] = encode_cells(self._cells)
        state["RealTimeMap"] = merged

    def has_map(self, state: dict) -> bool:
        return bool(((state.get("RealTimeMap") or {}).get("MapData"))
                    or ((state.get("RealMapData_1") or {}).get("MapData1")))

    def render_key(self, state: dict):
        rtm = (state or {}).get("RealTimeMap") or {}
        rmd = (state or {}).get("RealMapData_1") or {}
        return (rtm.get("MapData"), rtm.get("CurrentPiont"), (state or {}).get("ChargerPiont"),
                rmd.get("UpdateTime"), rmd.get("MapData1"))

    def render(self, state: dict) -> bytes | None:
        from .map import render_png

        return render_png(state)

    def diagnostics(self) -> dict:
        return {"key": self.key, "live_cells": len(self._cells)}


class SlamX9Protocol(MapProtocol):
    """SLAM "X9" models (mapType 3): ILIFE A10 / L100, AVA PRO MAX... Saved map of the
    selected map with rooms, live cleaned area + path, room / zone / go-to commands.
    Formats in map_x9.py."""

    key = "slam_x9"
    rooms = zones = goto = commands = True
    # an incomplete multi-pack saved map is fetched again this many polls, then drawn as is
    PACK_ATTEMPTS = 3

    def __init__(self, api) -> None:
        super().__init__(api)
        self.map_id: int | None = None
        self.map_name: str | None = None
        self.room_list: list[dict] = []
        self.saved: dict[tuple[int, int], int] = {}
        self.live: dict[tuple[int, int], int] = {}
        self.path: list[tuple[int, int]] = []
        self.robot: tuple[int, int] | None = None
        self.charger: tuple[int, int] | None = None
        self._saved_key: Any = None
        self._pack_tries = 0
        self._rooms_key: Any = None
        self._road_session: Any = None
        self._road_raw: Any = None
        self._layout = None
        self._layout_key: Any = None
        # what the executor renders: replaced as a whole, never mutated in place
        self._snapshot: tuple = ({}, {}, (), None, None)

    async def async_update(self, hass, state: dict) -> None:
        """Saved map (re-decoded only when it changes), rooms, live area, path, positions."""
        from . import map_x9

        map_id = map_x9.selected_map_id(state)
        key, slot = map_x9.saved_map_slot(state, map_id)
        skey = (map_id, slot.get("TimeStamp")) if slot else None
        if skey != self._saved_key:
            items, complete = ([slot], True) if slot else ([], True)
            if slot and map_x9.pack_count(slot) > 1:
                try:
                    items = await hass.async_add_executor_job(
                        self.api.map_packs, key, map_id, slot.get("TimeStamp")) or [slot]
                except ILifeError:
                    _LOGGER.debug("ILIFE saved map packs unavailable", exc_info=True)
                self._pack_tries += 1
                complete = (len(items) >= map_x9.pack_count(slot)
                            or self._pack_tries >= self.PACK_ATTEMPTS)
            self.saved = (await hass.async_add_executor_job(
                map_x9.decode_grid, map_x9.join_packs(items, "MapData", 7)))[0] if items else {}
            if complete:  # incomplete: fetched again next poll, a few times
                self._saved_key = skey
                self._pack_tries = 0
        self.map_id = map_id
        self.map_name, names = map_x9.room_info(state, map_id)
        if (skey, len(self.saved), names) != self._rooms_key:
            self._rooms_key = (skey, len(self.saved), names)
            self.room_list = map_x9.build_rooms(self.saved, names)

        working = state.get("WorkMode") in CLEANING_MODES | PAUSED_MODES
        session = state.get("RealTimeMapStart")
        self.live = {}
        # Without a saved map, the last clean's live map is all there is to show, which
        # is what these models showed before they had a protocol of their own.
        if working or not self.saved:
            packs = map_x9.live_packs(state, session)
            if packs:
                self.live = (await hass.async_add_executor_job(
                    map_x9.decode_grid, map_x9.join_packs(packs, "MapData", 8)))[0]

        road = state.get("RealMapRoadData")
        road = road if isinstance(road, dict) else {}
        road_session = state.get("RealTimeRoadStart")
        if road_session != self._road_session:
            self._road_session = road_session
            self._road_raw = None
            self.path = []
        raw = road.get("RoadData")
        if working and raw and raw != self._road_raw:
            # RoadData is a small rolling window: append only what is new in it
            self._road_raw = raw
            self.path = map_x9.extend_path(self.path, map_x9.decode_points(raw))[-5000:]
        self.robot = map_x9.unpack_point(road.get("CurrentPoint"))
        charger = state.get("ChargerPoint")
        self.charger = map_x9.unpack_point(charger.get("Piont") if isinstance(charger, dict) else None)

        self._snapshot = (self.saved, self.live, tuple(self.path), self.robot, self.charger)
        lkey = (self.map_id, len(self.saved), None if self.saved else (len(self.live), self.robot))
        if lkey != self._layout_key:
            self._layout_key = lkey
            self._layout = await hass.async_add_executor_job(
                map_x9.layout_for, self.saved, self.live, self.robot, self.charger)

    def has_map(self, state: dict) -> bool:
        return bool(self.saved or self.live)

    def render_key(self, state: dict):
        return (self.map_id, len(self.saved), len(self.live), len(self.path), self.robot, self.charger)

    def render(self, state: dict) -> bytes | None:
        from .map_x9 import render_png

        saved, live, path, robot, charger = self._snapshot
        return render_png(saved, live, list(path), robot, charger)[0]

    def camera_attributes(self) -> dict | None:
        lay = self._layout
        if lay is None:
            return None
        rooms = {}
        for r in self.room_list:
            if r["x"] is None:
                continue
            px, py = lay.center(r["x"], r["y"])
            rooms[str(r["id"])] = {"name": r["name"], "x": r["x"], "y": r["y"],
                                   "map_x": round(px), "map_y": round(py)}
        return {"calibration_points": lay.calibration(), "rooms": rooms}

    def vacuum_attributes(self) -> dict | None:
        # same shape as the ILIFE Clean vacuums' `rooms`: {room id: name as in the app}
        return {"map_id": self.map_id, "map_name": self.map_name,
                "rooms": {r["id"]: r["name"] for r in self.room_list}}

    def diagnostics(self) -> dict:
        return {
            "key": self.key,
            "map_id": self.map_id,
            "saved_cells": len(self.saved),
            "live_cells": len(self.live),
            "path_points": len(self.path),
            "room_ids": [r["id"] for r in self.room_list],
            # how many of them the app has names for (MapRoomInfo)
            "named_rooms": len(self._rooms_key[2]) if self._rooms_key else 0,
        }

    def require_map(self, capability: str) -> None:
        # rooms need the app's room segmentation; zones and go-to only a saved map
        if not (self.room_list if capability == "rooms" else self.saved):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_saved_map")

    def room_mask(self, rooms) -> int:
        """Room ids (bit values, see the `rooms` attribute) or unique names -> bitmask."""
        self.require_map("rooms")
        known = self.room_list
        listing = ", ".join(f"{r['name']} ({r['id']})" for r in known)
        ids = {r["id"] for r in known}
        mask = 0
        for item in rooms:
            if isinstance(item, int) or str(item).strip().isdigit():
                hits = [int(item)] if int(item) in ids else []
            else:
                hits = [r["id"] for r in known if r["name"].casefold() == str(item).strip().casefold()]
            if len(hits) != 1:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="room_ambiguous" if hits else "room_unknown",
                    translation_placeholders={"room": str(item), "rooms": listing or "none"},
                )
            mask |= hits[0]
        if not mask:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="room_unknown",
                translation_placeholders={"room": "", "rooms": listing or "none"})
        return mask


def _reached_clean_start(items: list) -> bool:
    """Newest-first paging can stop once the items include a CleanTime drop."""
    from .map_w450 import current_clean

    return len(current_clean(items)) < len(items)


class W450Protocol(MapProtocol):
    """W450 / W400 / W455 floor washers (mapType 4). Like the app, the map is rebuilt
    from the whole RealTimeMap / RealMapRoadData timeline of the current clean (polling
    only sees a fraction of the posts), drawn with the W450 cell types and orientation
    (map_w450.py)."""

    key = "w450"
    # Fetch new posts while the robot is out; idle/docked/asleep -> nothing new comes.
    _QUIET_MODES = {0, 1, 2, 9, 11, 16, 17}
    _IDLE_REFRESH = 1800  # s: still re-check now and then (a clean started from a button)

    def __init__(self, api) -> None:
        super().__init__(api)
        from .map_w450 import W450Map

        self.map = W450Map()
        self.charger = None
        self._session: Any = None
        self._map_ts = 0
        self._road_ts = 0
        self._was_active = False
        self._last_fetch = 0.0
        self._version = 0

    async def async_update(self, hass, state: dict) -> None:
        import time as _time

        from .map_w450 import charger_point, current_clean

        self.charger = charger_point(state.get("ChargerPiont"))
        times = getattr(self.api, "prop_times", {}) or {}
        map_start = int(times.get("RealTimeMapStart") or 0)
        road_start = int(times.get("RealTimeRoadStart") or map_start)
        session = (state.get("RealTimeMapStart"), map_start)
        active = state.get("WorkMode") not in self._QUIET_MODES
        now = _time.time()
        if session != self._session:
            # new clean (or first poll): rebuild the whole map, as the app does on open
            self.map.reset()
            self._map_ts = map_start - 1 if map_start else int(now * 1000) - 86_400_000
            self._road_ts = road_start - 1 if road_start else self._map_ts
            self._session = session
        elif not (active or self._was_active or now - self._last_fetch > self._IDLE_REFRESH):
            return
        self._was_active = active
        self._last_fetch = now
        end = int(now * 1000) + 60_000
        if self.map.empty() and self._map_ts < end - 60_000:
            # full load: newest first, back to where the latest clean began (the robot
            # does not always re-report RealTimeMapStart, so it can be days old)
            maps = await hass.async_add_executor_job(
                functools.partial(self.api.property_timeline, "RealTimeMap", self._map_ts + 1,
                                  end, ordered=False, stop=_reached_clean_start))
            maps = current_clean(maps)
            if maps:
                first = int(maps[0].get("timestamp") or 0)
                self._road_ts = max(self._road_ts, first - 1)
        else:
            maps = await hass.async_add_executor_job(
                self.api.property_timeline, "RealTimeMap", self._map_ts + 1, end)
        roads = await hass.async_add_executor_job(
            self.api.property_timeline, "RealMapRoadData", self._road_ts + 1, end)
        # one stream, in time order: a CleanTime drop in either resets the map
        merged = sorted([(int(i.get("timestamp") or 0), 0, i) for i in maps]
                        + [(int(i.get("timestamp") or 0), 1, i) for i in roads],
                        key=lambda e: (e[0], e[1]))
        for ts, kind, item in merged:
            if kind == 0:
                self.map.add_map_post(item)
                self._map_ts = max(self._map_ts, ts)
            else:
                self.map.add_road_post(item)
                self._road_ts = max(self._road_ts, ts)
        if merged:
            self._version += 1

    def has_map(self, state: dict) -> bool:
        return not self.map.empty()

    def render_key(self, state: dict):
        return (self._version, self.charger)

    def render(self, state: dict) -> bytes | None:
        from .map_w450 import render_png

        return render_png(self.map, self.charger)

    def camera_attributes(self) -> dict | None:
        # The camera keeps the whole last clean (rebuilt from the timeline), which is
        # better than the history thumbnail: calibration_points tell the card to keep
        # showing it when the robot is docked, and suit xiaomi-vacuum-map-card.
        from .map_w450 import calibration

        cal = calibration(self.map, self.charger) if not self.map.empty() else None
        return {"calibration_points": cal} if cal else None

    @staticmethod
    def assemble_history(packs: dict) -> str | None:
        """CleanHistory packs -> CleanMapData the card and gallery can draw."""
        from .map_w450 import decode_history, to_clean_map

        return to_clean_map(decode_history(packs)[0])

    def diagnostics(self) -> dict:
        types: dict[int, int] = {}
        for t in self.map.cells.values():
            types[t] = types.get(t, 0) + 1
        return {"key": self.key, "cells": len(self.map.cells), "cell_types": types,
                "path_segments": len(self.map.paths)}


def product(device: dict) -> tuple[str, str, int] | None:
    return PRODUCTS.get((device or {}).get("productKey"))


def detect(device: dict, state: dict | None) -> type[MapProtocol]:
    """Known productKey -> its mapType; unknown -> from the reported properties."""
    p = product(device)
    if p is not None:
        return {3: SlamX9Protocol, 4: W450Protocol}.get(p[2], RealTimeMapProtocol)
    if state and any(k in state for k in _SLAM_PROPS):
        return SlamX9Protocol
    return RealTimeMapProtocol
