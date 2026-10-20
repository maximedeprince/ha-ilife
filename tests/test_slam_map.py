"""Saved map, rooms and map commands of ILIFEHOME SLAM models (ILIFE A10 / L100,
AVA PRO MAX...).

The map below is synthetic, built with the layout the app parses
(DataUtils.parseSaveMapDataX9); the zone bytes are the ones an AVA PRO MAX app sent.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import pathlib
import struct
import sys

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_spec = importlib.util.spec_from_file_location("_ilife_map_x9", _PKG / "map_x9.py")
map_x9 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = map_x9
_spec.loader.exec_module(map_x9)

A, B, W, ROWS = 5, 10, 20, 10


def _grid():
    """Walls around two rooms: value 11 (room bit 1) left, 12 (room bit 2) right."""
    rows = []
    for r in range(ROWS):
        row = []
        for c in range(W):
            if r in (0, ROWS - 1) or c in (0, W - 1):
                row.append(map_x9.WALL)
            else:
                row.append(11 if c < W // 2 else 12)
        rows.append(row)
    return rows


def _rle(cells, kind=1):
    out = bytearray([kind, 50]) + struct.pack(">hhh", A, B, W)
    flat = [v for row in cells for v in row]
    i = 0
    while i < len(flat):
        n = 1
        while i + n < len(flat) and flat[i + n] == flat[i] and n < 255:
            n += 1
        out += bytes([flat[i], n])
        i += n
    return bytes(out)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _slot(raw, pack=(1, 1), ts=1700000000, map_id=1700000000):
    return {"MapId": map_id, "TimeStamp": ts, "PackId": (pack[0] << 16) | pack[1],
            "MapData1": _b64(raw)}


class TestGrid:
    def test_cells_are_in_map_coordinates(self):
        cells, res = map_x9.decode_grid(_rle(_grid()))
        assert res == 50
        assert len(cells) == W * ROWS
        # row 0, col 0 is x = A - 0, y = 0 - B
        assert cells[(A, -B)] == map_x9.WALL
        assert cells[(A - 1, 1 - B)] == 11
        assert cells[(A - 1, W - 2 - B)] == 12

    def test_unknown_cells_are_skipped_but_counted(self):
        grid = _grid()
        grid[1] = [map_x9.UNKNOWN] * W
        cells, _ = map_x9.decode_grid(_rle(grid))
        assert (A - 1, 1 - B) not in cells
        assert cells[(A - 2, 1 - B)] == 11  # the next row is still in place

    def test_later_packs_repeat_the_header(self):
        raw = _rle(_grid())
        body = raw[8:]
        cut = (len(body) // 4) * 2  # on a (value, count) boundary
        packs = [_slot(raw[:8] + body[:cut], (1, 2)), _slot(raw[:8] + body[cut:], (2, 2))]
        joined = map_x9.join_packs(list(reversed(packs)), "MapData", 7)
        assert joined == raw
        assert map_x9.pack_count(packs[0]) == 2


class TestRooms:
    def test_rooms_from_the_grid_and_the_app_names(self):
        state = {
            "SaveMap": {"SelectedMapId": 1700000000, "SaveMapId": ""},
            "SaveMapDataX9_2": _slot(_rle(_grid())),
            "MapRoomInfo1": _b64(b"1600000000,old,1,Old room"),
            "MapRoomInfo2": _b64(b"1700000000,home,1,Living room,2,Kitchen"),
        }
        map_id = map_x9.selected_map_id(state)
        key, slot = map_x9.saved_map_slot(state, map_id)
        assert key == "SaveMapDataX9_2"
        cells, _ = map_x9.decode_grid(map_x9.join_packs([slot], "MapData", 7))
        name, names = map_x9.room_info(state, map_id)
        assert (name, names) == ("home", {1: "Living room", 2: "Kitchen"})
        rooms = map_x9.build_rooms(cells, names)
        assert [(r["id"], r["name"]) for r in rooms] == [(1, "Living room"), (2, "Kitchen")]
        living = rooms[0]
        assert living["x"] == round(A - ROWS / 2 + 0.5)
        assert living["y"] < rooms[1]["y"]

    def test_unnamed_room_gets_a_generic_name(self):
        cells, _ = map_x9.decode_grid(_rle(_grid()))
        rooms = map_x9.build_rooms(cells, {1: "Living room"})
        assert rooms[1]["name"] == "Room 2"

    def test_names_may_contain_commas(self):
        state = {"MapRoomInfo1": _b64("5,Home, upstairs,1,Bath, small,4,Hall".encode())}
        assert map_x9.room_info(state, 5) == ("Home, upstairs", {1: "Bath, small", 4: "Hall"})

    def test_no_names_for_another_map(self):
        assert map_x9.room_info({"MapRoomInfo1": _b64(b"5,home,1,A")}, 6) == (None, {})


class TestCommands:
    def test_zone_is_encoded_as_the_app_sends_it(self):
        # corners (xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin), any input order
        assert map_x9.encode_rect(76, 31, -52, 159) == "/8wAnwBMAJ8ATAAf/8wAHw=="

    def test_point(self):
        assert base64.b64decode(map_x9.encode_point(27, -113)) == struct.pack(">hh", 27, -113)

    def test_packed_position(self):
        assert map_x9.unpack_point(-589825) == (-10, -1)
        assert map_x9.unpack_point(None) is None


class TestLiveMap:
    def test_a_live_map_older_than_the_session_is_the_previous_clean(self):
        state = {"RealMapData_1": {"UpdateTime": 100, "PackId": 65537, "MapData1": "AA=="}}
        assert map_x9.live_packs(state, session=200) == []
        assert map_x9.live_packs(state, session=50) == [state["RealMapData_1"]]

    def test_only_the_newest_upload_is_joined(self):
        new = {"UpdateTime": 300, "PackId": 65537}
        old = {"UpdateTime": 200, "PackId": (2 << 16) | 2}
        assert map_x9.live_packs({"RealMapData_1": new, "RealMapData_2": old}) == [new]

    def test_path_windows_overlap(self):
        path = map_x9.extend_path([], [(1, 1), (2, 2), (3, 3)])
        path = map_x9.extend_path(path, [(2, 2), (3, 3), (4, 4)])
        path = map_x9.extend_path(path, [(2, 2), (3, 3), (4, 4)])
        assert path == [(1, 1), (2, 2), (3, 3), (4, 4)]


class TestRender:
    def test_png_and_orientation(self):
        cells, _ = map_x9.decode_grid(_rle(_grid()))
        png, lay = map_x9.render_png(cells, {}, [(A - 1, 1 - B), (A - 2, 2 - B)],
                                     (A - 2, 2 - B), (A - 1, 1 - B))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # map x grows to the right, y downwards (the app's orientation)
        (x0, y0), (x1, y1) = lay.px(0, 0), lay.px(1, 1)
        assert x1 > x0 and y1 > y0
        assert lay.width == (ROWS + 8) * lay.cell and lay.height == (W + 8) * lay.cell
        pts = lay.calibration()
        assert len(pts) == 3 and pts[1]["map"]["x"] > pts[0]["map"]["x"]

    def test_nothing_to_draw(self):
        assert map_x9.render_png({}, {}, [], None, None) == (None, None)
