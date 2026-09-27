"""Regression tests for the ILIFE Clean (Tuya) map decoder: both known versions.

The maps here are *synthesized* to the format `tuya_map` documents: a real capture
is a floor plan of someone's home and does not belong in a public repository. So
what they pin down is the decoding, not the format — a misread of the format is
what the checks against the app's own rendering were for.

Two things are easy to break again and have a test each:
  * A30 Pro (version 1) walls and zones are cell offsets from the robot origin,
    not robot units and not grid cells;
  * the V20 (version 2) cell and 0xAA-frame encoding keeps working next to it.

Rule for malformed input: a named error, never an IndexError, a struct.error, or a
silently truncated image.

Pure functions, no Home Assistant needed: `python -m pytest tests/`.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import pathlib
import struct
import sys
import types

import pytest

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_pkg = types.ModuleType("_ilife_map")
_pkg.__path__ = [str(_PKG)]
sys.modules["_ilife_map"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ilife_map.{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tuya_lz4 = _load("tuya_lz4")
tuya_map = _load("tuya_map")


# --------------------------------------------------------------------------- #
# LZ4 block building, so the fixtures below are real compressed blocks
# --------------------------------------------------------------------------- #

def _length_bytes(value: int) -> bytes:
    """Encode a length above the 15 a token nibble can hold."""
    out = bytearray()
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)
    return bytes(out)


def _lz4_literals(data: bytes) -> bytes:
    """A valid LZ4 block that stores `data` uncompressed, as one literal run."""
    if len(data) < 15:
        return bytes([len(data) << 4]) + data
    return bytes([0xF0]) + _length_bytes(len(data) - 15) + data


class TestLZ4:
    def test_literal_only_block_round_trips(self):
        payload = bytes(range(256)) * 3
        assert tuya_lz4.decompress_block(_lz4_literals(payload), len(payload)) == payload

    def test_match_sequence_repeats_earlier_output(self):
        block = bytes([(4 << 4) | 4]) + b"abcd" + struct.pack("<H", 4) + _lz4_literals(b"!")
        assert tuya_lz4.decompress_block(block, 13) == b"abcdabcdabcd!"

    def test_declared_size_is_enforced(self):
        with pytest.raises(tuya_lz4.LZ4BlockError):
            tuya_lz4.decompress_block(_lz4_literals(b"four"), 99)

    def test_match_offset_before_the_start_is_rejected(self):
        block = bytes([(1 << 4) | 0]) + b"a" + struct.pack("<H", 9) + _lz4_literals(b"")
        with pytest.raises(tuya_lz4.LZ4BlockError):
            tuya_lz4.decompress_block(block)


# --------------------------------------------------------------------------- #
# Synthetic maps
# --------------------------------------------------------------------------- #

WIDTH, HEIGHT = 8, 6
RESOLUTION_CM = 5
UNITS = RESOLUTION_CM * 2
ORIGIN_X, ORIGIN_Y = 30, 40          # robot zero sits at grid cell (3, 4)
PILE_X, PILE_Y = 40, 40              # dock at grid cell (4, 4)
A30, V20 = tuya_map.VERSION_A30, tuya_map.VERSION_V20
OUT, WALL = tuya_map.CELL_OUTSIDE, tuya_map.CELL_OBSTACLE   # version 1 cell values


def _room_block(room_id: int, name: bytes = b"", vertices=()) -> bytes:
    block = bytearray(47)
    struct.pack_into(">4H", block, 0, room_id, 0, 0, 0)
    block[26] = len(name)
    block[27 : 27 + len(name)] = name
    block[46] = len(vertices)
    return bytes(block) + _points(vertices)


def _grid(assignments: dict[int, int] | None = None, fill: int = OUT) -> bytes:
    cells = bytearray([fill]) * (WIDTH * HEIGHT)
    for index, value in (assignments or {}).items():
        cells[index] = value
    return bytes(cells)


def _points(points) -> bytes:
    return b"".join(struct.pack(">hh", x, y) for x, y in points)


def _path_section(points, *, compress: bool = False) -> bytes:
    raw = _points(points)
    section = bytearray(13)
    section[5:9] = len(points).to_bytes(4, "big")
    if compress:
        block = _lz4_literals(raw)
        section[11:13] = len(block).to_bytes(2, "big")
        return bytes(section) + block
    return bytes(section) + raw


def _walls(walls) -> bytes:
    """Version 1 virtual-wall record."""
    return bytes([0x13, len(walls)]) + b"".join(_points(wall) for wall in walls)


def _zones(zones) -> bytes:
    """Version 1 no-go-zone record."""
    return b"\x1b" + len(zones).to_bytes(2, "big") + b"".join(_points(z) for z in zones)


def _frame(command: int, payload: bytes) -> bytes:
    """Version 2 0xAA frame."""
    body = bytes([command]) + payload
    return b"\xaa" + len(body).to_bytes(2, "big") + body + bytes([sum(body) & 0xFF])


def _map(*, version=A30, grid=None, rooms=(), trailer=b"", compressed_length=None) -> bytes:
    if grid is None:
        grid = _grid(fill=OUT if version == A30 else 0)
    body = grid + bytes([0, len(rooms)]) + b"".join(rooms)
    block = _lz4_literals(body)
    header = bytearray([version]) + (1).to_bytes(2, "big") + b"\x01"
    header += struct.pack(
        ">10H", WIDTH, HEIGHT, ORIGIN_X, ORIGIN_Y, RESOLUTION_CM,
        PILE_X & 0xFFFF, PILE_Y & 0xFFFF, 0, len(body),
        len(block) if compressed_length is None else compressed_length,
    )
    return bytes(header) + block + trailer


# --------------------------------------------------------------------------- #
# Both versions
# --------------------------------------------------------------------------- #

class TestHeader:
    @pytest.mark.parametrize("version", [A30, V20])
    def test_fields(self, version):
        header = tuya_map.decode_tuya_map(_map(version=version))["header"]
        assert (header["version"], header["width"], header["height"]) == (version, WIDTH, HEIGHT)
        assert (header["origin_x"], header["origin_y"]) == (ORIGIN_X, ORIGIN_Y)
        assert (header["pile_x"], header["pile_y"]) == (PILE_X, PILE_Y)

    def test_unknown_versions_say_so(self):
        with pytest.raises(NotImplementedError):
            tuya_map.decode_tuya_map(_map(version=3))

    def test_short_file(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(b"\x01\x00\x01")

    def test_compressed_length_past_the_buffer(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(compressed_length=4096))

    def test_zero_resolution(self):
        raw = bytearray(_map())
        struct.pack_into(">H", raw, 12, 0)
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(bytes(raw))


class TestRooms:
    def test_names_numbered_like_the_app_when_empty(self):
        rooms = tuya_map.decode_tuya_map(
            _map(rooms=[_room_block(0), _room_block(1, b"Kitchen", [(0, 0), (10, 10)])])
        )["rooms"]
        assert [room["name"] for room in rooms] == ["Room 1", "Kitchen"]
        assert rooms[1]["vertices"] == [[0, 0], [10, 10]]

    def test_truncated_room_metadata(self):
        raw = bytearray(_map(rooms=[_room_block(0)]))
        raw[24 + 2 + WIDTH * HEIGHT + 1] = 2   # claim a second room that is not there
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(bytes(raw))


class TestPath:
    def test_break_markers_flag_the_next_point_as_travel(self):
        brk = tuya_map.PATH_BREAK
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(0, 0), (10, 0), brk, (20, 0), brk, (30, 0)]))
        )
        assert decoded["path_points"] == [[0, 0], [10, 0], [20, 0], [30, 0]]
        assert decoded["path_travel"] == [False, False, True, True]

    def test_compressed_path(self):
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(1, 2), (3, 4)], compress=True))
        )
        assert decoded["path_points"] == [[1, 2], [3, 4]]

    def test_include_path_false_hides_it(self):
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(1, 2)])), include_path=False
        )
        assert decoded["path_points"] == []

    def test_separate_path_file_wins_and_bad_one_is_ignored(self):
        raw = _map(trailer=_path_section([(5, 5)]))
        assert tuya_map.decode_tuya_map(raw, _path_section([(1, 1)]))["path_points"] == [[1, 1]]
        assert tuya_map.decode_tuya_map(raw, b"\xff" * 14)["path_points"] == [[5, 5]]

    def test_implausible_point_count(self):
        section = bytearray(13)
        section[5:9] = (9_000_000).to_bytes(4, "big")
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(trailer=bytes(section)))

    def test_truncated_path(self):
        section = bytearray(13)
        section[5:9] = (100).to_bytes(4, "big")
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(trailer=bytes(section)))


class TestCoordinates:
    HEADER = {"origin_x": ORIGIN_X, "origin_y": ORIGIN_Y, "resolution_cm": RESOLUTION_CM}

    def test_robot_units_are_y_up_from_the_origin(self):
        assert tuya_map._robot_to_cell([0, 0], self.HEADER) == (3, 4)
        assert tuya_map._robot_to_cell([UNITS, UNITS], self.HEADER) == (4, 3)

    def test_zero_resolution(self):
        with pytest.raises(ValueError):
            tuya_map._robot_to_cell([0, 0], {**self.HEADER, "resolution_cm": 0})


# --------------------------------------------------------------------------- #
# Version 1 (A30 Pro)
# --------------------------------------------------------------------------- #

class TestA30:
    WALL_CELLS = [(1, -1), (1, 1)]
    ZONE_CELLS = [(-2, 0), (-2, 1), (0, 1), (0, 0)]

    def test_cells_are_room_id_shifted_by_two_with_flags_ignored(self):
        grid = _grid({0: 0 << 2, 1: 0 << 2 | 1, 2: 1 << 2, 3: 1 << 2 | 3, 4: 1 << 2, 5: WALL})
        decoded = tuya_map.decode_tuya_map(_map(grid=grid, rooms=[_room_block(0), _room_block(1)]))
        cell = RESOLUTION_CM ** 2 / 10_000
        assert decoded["room_areas_m2"] == {0: round(2 * cell, 2), 1: round(3 * cell, 2)}
        assert decoded["cells"][5] == (tuya_map.KIND_WALL, None)
        assert decoded["cells"][6] == (tuya_map.KIND_OUTSIDE, None)

    def test_walls_and_zones_are_cell_offsets_from_the_robot_origin(self):
        # One cell right and one up is UNITS robot units each way, not 1.
        decoded = tuya_map.decode_tuya_map(_map(trailer=(
            _path_section([(0, 0)], compress=True)
            + _walls([self.WALL_CELLS]) + _zones([self.ZONE_CELLS])
        )))
        assert decoded["virtual_walls"] == [[[x * UNITS, y * UNITS] for x, y in self.WALL_CELLS]]
        assert decoded["no_go_zones"] == [[[x * UNITS, y * UNITS] for x, y in self.ZONE_CELLS]]

    def test_record_bytes_inside_the_path_are_not_mistaken_for_records(self):
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(0x1302, 0x1B00), (0x1301, 0x13)]))
        )
        assert decoded["virtual_walls"] == [] and decoded["no_go_zones"] == []

    def test_parsing_stops_at_an_unknown_tag(self):
        # The A30 Pro ends the file with a trailer of unknown meaning.
        trailer = _path_section([]) + _walls([self.WALL_CELLS]) + bytes.fromhex("0200010500020100")
        assert len(tuya_map.decode_tuya_map(_map(trailer=trailer))["virtual_walls"]) == 1

    def test_truncated_record(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(
                _map(trailer=_path_section([]) + _walls([self.WALL_CELLS])[:-3]))


# --------------------------------------------------------------------------- #
# Version 2 (V20)
# --------------------------------------------------------------------------- #

class TestV20:
    def _two_rooms(self):
        # 3 cells of room 1, 2 of room 2, one wall, one carpet.
        grid = _grid({
            0: (1 << 3) | 0x07, 1: (1 << 3) | 0x07, 2: (1 << 3) | 0x07,
            8: (2 << 3) | 0x07, 9: (2 << 3) | 0x07, 16: 0x01, 17: 0x02,
        }, fill=0)
        rooms = [_room_block(1, b"Kitchen"), _room_block(2, b"Hall")]
        return tuya_map.decode_tuya_map(_map(version=V20, grid=grid, rooms=rooms))

    def test_cells_and_areas(self):
        decoded = self._two_rooms()
        cell = RESOLUTION_CM ** 2 / 10_000
        assert decoded["room_areas_m2"] == {1: round(3 * cell, 2), 2: round(2 * cell, 2)}
        assert decoded["cells"][16] == (tuya_map.KIND_WALL, None)
        assert decoded["cells"][17] == (tuya_map.KIND_CARPET, None)
        assert decoded["cells"][20] == (tuya_map.KIND_OUTSIDE, None)

    def test_walls_and_zones_come_from_frames_in_robot_units(self):
        walls = _frame(0x13, bytes([1]) + _points([(0, 0), (100, 100)]))
        zone = _frame(0x1B, bytes([1, 0, 4]) + _points([(0, 0), (10, 0), (10, 10), (0, 10)]))
        decoded = tuya_map.decode_tuya_map(
            _map(version=V20, trailer=_path_section([]) + walls + zone))
        assert decoded["virtual_walls"] == [[[0, 0], [100, 100]]]
        assert decoded["no_go_zones"] == [[[0, 0], [10, 0], [10, 10], [0, 10]]]

    def test_a_frame_with_a_bad_checksum_is_ignored(self):
        frame = bytearray(_frame(0x13, bytes([1]) + _points([(0, 0), (1, 1)])))
        frame[-1] ^= 0xFF
        decoded = tuya_map.decode_tuya_map(
            _map(version=V20, trailer=_path_section([]) + bytes(frame)))
        assert decoded["virtual_walls"] == []


# --------------------------------------------------------------------------- #
# Rendering and history
# --------------------------------------------------------------------------- #

class TestRendering:
    @pytest.mark.parametrize("version", [A30, V20])
    def test_full_map(self, version):
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        floor = 0 if version == A30 else 0x07
        grid = _grid({i: floor for i in range(8, 40)}, fill=OUT if version == A30 else 0)
        if version == A30:
            overlays = _walls([TestA30.WALL_CELLS]) + _zones([TestA30.ZONE_CELLS])
        else:
            overlays = _frame(0x13, bytes([1]) + _points([(0, 0), (UNITS, UNITS)]))
        png, metadata = tuya_map.render_tuya_map_png(_map(
            version=version, grid=grid, rooms=[_room_block(0)],
            trailer=_path_section([(0, 0), (10, 10)]) + overlays,
        ))
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert metadata["rooms"] == {0: "Room 1"}
        assert metadata["path_points"] == 2 and metadata["virtual_walls"] == 1
        assert all(not isinstance(value, bytes) for value in metadata.values())

    def test_room_names_from_the_app_replace_the_numbered_ones(self):
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        _, metadata = tuya_map.render_tuya_map_png(
            _map(grid=_grid({8: 0, 9: 1 << 2}), rooms=[_room_block(0), _room_block(1)]),
            room_names={1: "Kitchen"},
        )
        assert metadata["rooms"] == {0: "Room 1", 1: "Kitchen"}

    def test_points_off_the_grid_break_the_path_instead_of_being_drawn(self):
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        _, metadata = tuya_map.render_tuya_map_png(
            _map(trailer=_path_section([(0, 0), (10, 0), (-30000, 30000), (10, 10)]))
        )
        assert (metadata["path_points"], metadata["path_points_off_map"]) == (4, 1)

    def test_hidden_path(self):
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        _, metadata = tuya_map.render_tuya_map_png(
            _map(trailer=_path_section([(0, 0), (10, 10)])), include_path=False
        )
        assert metadata["path_points"] == 0

    def test_labels_fold_what_the_builtin_font_cannot_draw(self):
        assert tuya_map._fold_to_latin1("Łazienka żółta") == "Lazienka zólta"


class TestHistory:
    def test_thumbnail_uses_the_cards_format(self):
        grid = _grid({0: 0, 1: 0, 2: WALL, 8: 1 << 2})
        raw = base64.b64decode(tuya_map.clean_map_thumbnail(_map(grid=grid), factor=1))
        assert raw[:2] == bytes([0x01, 2])              # format, bytes per row (8 cells)
        assert len(raw) == 2 + 2 * HEIGHT
        assert raw[2] == 0b01_01_10_00                  # floor, floor, wall, outside
        assert raw[4] >> 6 == 1                          # row 1, cell 0: room 1 floor

    def test_path_keeps_only_the_cleaning_runs(self):
        brk = tuya_map.PATH_BREAK
        points = [(0, 0), (UNITS, 0), (2 * UNITS, 0), brk, (0, UNITS), (0, 0)]
        runs = tuya_map.clean_map_path(_map(trailer=_path_section(points)), factor=1)
        assert runs == [[3, 4, 4, 4, 5, 4], [3, 3, 3, 4]]


CAPTURE = os.environ.get("ILIFE_A30_CAPTURE")


@pytest.mark.skipif(not CAPTURE, reason="set ILIFE_A30_CAPTURE to a private A30 Pro map file")
def test_private_capture_decodes_and_renders():
    pytest.importorskip("PIL")
    raw = pathlib.Path(CAPTURE).read_bytes()
    decoded = tuya_map.decode_tuya_map(raw)
    assert decoded["rooms"] and decoded["path_points"]
    png, _ = tuya_map.render_tuya_map_png(raw)
    assert png.startswith(b"\x89PNG")
