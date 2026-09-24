"""Regression tests for the ILIFE Clean (Tuya) V2 map decoder.

Unlike `test_tuya_commands.py`, these are not built from a captured device file:
the V20 map that motivated this decoder is a floor plan of someone's home and does
not belong in a public repository. So the maps here are *synthesized* to the format
`tuya_map` documents, and what they pin down is the decoding, not the format.

That still covers the failure that actually costs a user their afternoon. A map
arrives as an LZ4 block with lengths declared in its own header, from a cloud that
owes us nothing: every one of those lengths is attacker- or firmware-controlled and
can point past the end of the buffer. The rule these tests enforce is:

    a malformed or unsupported map raises a named error, never an IndexError,
    a struct.error, or a silently truncated image.

A real capture should be added on top of this the day one can be shared; it would
catch a misread of the format, which by construction these cannot.

Pure functions, no Home Assistant needed: `python -m pytest tests/`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import struct
import sys
import types

import pytest

_PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
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
        # "abcd" literally, then a match of 8 bytes at offset 4 -> "abcd" twice more,
        # then a literal-only sequence to close the block as the format requires.
        block = bytes([(4 << 4) | 4]) + b"abcd" + struct.pack("<H", 4) + _lz4_literals(b"!")
        assert tuya_lz4.decompress_block(block, 13) == b"abcdabcdabcd!"

    def test_overlapping_match_is_a_run(self):
        # Offset 1 with a length beyond it is how LZ4 encodes a repeated byte.
        block = bytes([(1 << 4) | 2]) + b"x" + struct.pack("<H", 1) + _lz4_literals(b"")
        assert tuya_lz4.decompress_block(block, 7) == b"xxxxxxx"

    def test_declared_size_is_enforced(self):
        with pytest.raises(tuya_lz4.LZ4BlockError):
            tuya_lz4.decompress_block(_lz4_literals(b"four"), 99)

    def test_match_offset_before_the_start_is_rejected(self):
        block = bytes([(1 << 4) | 0]) + b"a" + struct.pack("<H", 9) + _lz4_literals(b"")
        with pytest.raises(tuya_lz4.LZ4BlockError):
            tuya_lz4.decompress_block(block)

    def test_truncated_literal_run_is_rejected(self):
        with pytest.raises(tuya_lz4.LZ4BlockError):
            tuya_lz4.decompress_block(bytes([8 << 4]) + b"only3")


# --------------------------------------------------------------------------- #
# Synthetic V2 maps
# --------------------------------------------------------------------------- #

WIDTH, HEIGHT = 8, 4
RESOLUTION_CM = 5
ORIGIN_X, ORIGIN_Y = 40, 30
PILE_X, PILE_Y = 20, -10


def _room_block(room_id: int, name: bytes, vertices: list[tuple[int, int]]) -> bytes:
    """One 47-byte room record plus its vertex list."""
    block = bytearray(47)
    struct.pack_into(">4H", block, 0, room_id, 1, 2, 3)  # id, order, sweeps, mops
    block[8:14] = bytes((room_id, 0, 0, 1, 2, 0))        # color, forbids, fan, water, y
    block[26] = len(name)
    block[27 : 27 + len(name)] = name
    block[46] = len(vertices)
    return bytes(block) + b"".join(struct.pack(">hh", x, y) for x, y in vertices)


def _grid(assignments: dict[int, int]) -> bytes:
    """A WIDTH*HEIGHT grid of `(room_id << 3) | pixel_type` cells."""
    cells = bytearray(WIDTH * HEIGHT)
    for index, value in assignments.items():
        cells[index] = value
    return bytes(cells)


def _aa_frame(command: int, payload: bytes) -> bytes:
    body = bytes([command]) + payload
    return b"\xaa" + len(body).to_bytes(2, "big") + body + bytes([sum(body) & 0xFF])


def _path_section(points: list[tuple[int, int]]) -> bytes:
    """An uncompressed embedded path section (13-byte preamble, then points)."""
    section = bytearray(13)
    section[5:9] = len(points).to_bytes(4, "big")
    section[11:13] = (0).to_bytes(2, "big")  # 0 = points follow uncompressed
    return bytes(section) + b"".join(struct.pack(">hh", x, y) for x, y in points)


def _map(
    *,
    version: int = 2,
    grid: bytes | None = None,
    rooms: bytes = b"",
    room_count: int | None = None,
    trailer: bytes = b"",
    compressed_length: int | None = None,
    decompressed_length: int | None = None,
) -> bytes:
    grid = _grid({}) if grid is None else grid
    body = grid + (bytes([0, room_count if room_count is not None else 0]) + rooms if rooms or room_count else b"")
    block = _lz4_literals(body)
    header = bytearray()
    header.append(version)
    header += (7).to_bytes(2, "big")                   # map id
    header.append(0)                                   # type
    header += struct.pack(
        ">10H",
        WIDTH, HEIGHT,
        ORIGIN_X, ORIGIN_Y,
        RESOLUTION_CM,
        PILE_X & 0xFFFF, PILE_Y & 0xFFFF,
        0,
        len(body) if decompressed_length is None else decompressed_length,
        len(block) if compressed_length is None else compressed_length,
    )
    return bytes(header) + block + trailer


class TestMapHeader:
    def test_header_fields_are_read(self):
        decoded = tuya_map.decode_tuya_map(_map())
        header = decoded["header"]
        assert header["version"] == 2
        assert header["map_id"] == 7
        assert (header["width"], header["height"]) == (WIDTH, HEIGHT)
        assert header["resolution_cm"] == RESOLUTION_CM
        assert (header["pile_x"], header["pile_y"]) == (PILE_X, PILE_Y)

    def test_negative_coordinates_survive_the_unsigned_header(self):
        # pile_y is stored unsigned; -10 must not come back as 65526.
        assert tuya_map.decode_tuya_map(_map())["header"]["pile_y"] == -10

    def test_unsupported_version_says_so(self):
        with pytest.raises(NotImplementedError):
            tuya_map.decode_tuya_map(_map(version=3))

    def test_map_shorter_than_its_header_is_rejected(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(b"\x02\x00\x07\x00short")

    def test_compressed_length_past_the_buffer_is_rejected(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(compressed_length=4096))

    def test_body_smaller_than_the_declared_grid_is_rejected(self):
        # A header claiming 8x4 over a body that cannot hold 32 cells.
        payload = _lz4_literals(b"tiny")
        header = bytearray(_map()[:24])
        struct.pack_into(">H", header, 20, len(b"tiny"))
        struct.pack_into(">H", header, 22, len(payload))
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(bytes(header) + payload)


class TestRoomsAndAreas:
    def _two_rooms(self):
        rooms = _room_block(1, b"Kitchen", [(0, 0), (10, 0), (10, 10)]) + _room_block(
            2, b"Hall", [(20, 20), (30, 30)]
        )
        # 3 cells of room 1, 2 of room 2, one wall, one carpet.
        grid = _grid({
            0: (1 << 3) | 0x07, 1: (1 << 3) | 0x07, 2: (1 << 3) | 0x07,
            8: (2 << 3) | 0x07, 9: (2 << 3) | 0x07,
            16: 0x01, 17: 0x02,
        })
        return tuya_map.decode_tuya_map(_map(grid=grid, rooms=rooms, room_count=2))

    def test_room_names_and_vertices(self):
        rooms = self._two_rooms()["rooms"]
        assert [room["name"] for room in rooms] == ["Kitchen", "Hall"]
        assert rooms[0]["vertices"] == [[0, 0], [10, 0], [10, 10]]
        assert rooms[1]["vertices"] == [[20, 20], [30, 30]]

    def test_room_areas_use_the_header_resolution(self):
        # 3 cells at 5cm x 5cm = 75 cm2 = 0.01 m2 (rounded to 2 decimals).
        areas = self._two_rooms()["room_areas_m2"]
        assert areas[1] == round(3 * RESOLUTION_CM ** 2 / 10_000, 2)
        assert areas[2] == round(2 * RESOLUTION_CM ** 2 / 10_000, 2)

    def test_truncated_room_metadata_is_rejected(self):
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(rooms=b"\x00" * 10, room_count=1))

    def test_truncated_room_vertices_are_rejected(self):
        claims_four_vertices = bytearray(_room_block(1, b"X", []))
        claims_four_vertices[46] = 4
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(rooms=bytes(claims_four_vertices), room_count=1))


class TestPathAndTrailer:
    def test_embedded_path_points_are_read(self):
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(0, 0), (10, -20), (30, 40)]))
        )
        assert decoded["path_points"] == [[0, 0], [10, -20], [30, 40]]

    def test_a_separate_path_file_wins_over_the_embedded_one(self):
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(0, 0)])),
            _path_section([(1, 1), (2, 2)]),
        )
        assert decoded["path_points"] == [[1, 1], [2, 2]]

    def test_an_unreadable_path_file_falls_back_to_the_embedded_path(self):
        # A separate file that cannot be parsed must not lose the path we already have.
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([(5, 5)])), b"\xff" * 13 + b"\x00"
        )
        assert decoded["path_points"] == [[5, 5]]

    def test_virtual_walls_and_no_go_zones(self):
        walls = _aa_frame(0x13, bytes([1]) + struct.pack(">hhhh", 0, 0, 100, 100))
        zone = _aa_frame(
            0x1B,
            bytes([1, 0, 4]) + b"".join(
                struct.pack(">hh", x, y) for x, y in [(0, 0), (10, 0), (10, 10), (0, 10)]
            ),
        )
        decoded = tuya_map.decode_tuya_map(
            _map(trailer=_path_section([]) + walls + zone)
        )
        assert decoded["virtual_walls"] == [[[0, 0], [100, 100]]]
        assert decoded["no_go_zones"] == [
            {"index": 1, "type": 0, "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}
        ]
        assert decoded["embedded_commands"] == ["0x13", "0x1B"]

    def test_a_frame_with_a_bad_checksum_is_ignored(self):
        frame = bytearray(_aa_frame(0x13, bytes([1]) + struct.pack(">hhhh", 0, 0, 1, 1)))
        frame[-1] ^= 0xFF
        decoded = tuya_map.decode_tuya_map(_map(trailer=_path_section([]) + bytes(frame)))
        assert decoded["virtual_walls"] == []
        assert decoded["embedded_commands"] == []

    def test_an_implausible_point_count_is_rejected(self):
        section = bytearray(13)
        section[5:9] = (9_000_000).to_bytes(4, "big")
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(trailer=bytes(section)))

    def test_a_truncated_path_is_rejected(self):
        section = bytearray(13)
        section[5:9] = (100).to_bytes(4, "big")
        with pytest.raises(ValueError):
            tuya_map.decode_tuya_map(_map(trailer=bytes(section) + b"\x00\x00"))


class TestRendering:
    def test_a_full_map_renders_to_a_png(self):
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        rooms = _room_block(1, b"Kitchen", [(0, 0), (10, 10)])
        grid = _grid({0: (1 << 3) | 0x07, 1: (1 << 3) | 0x07, 8: 0x01, 9: 0x02})
        walls = _aa_frame(0x13, bytes([1]) + struct.pack(">hhhh", 0, 0, 40, 40))
        png, metadata = tuya_map.render_tuya_map_png(
            _map(
                grid=grid,
                rooms=rooms,
                room_count=1,
                trailer=_path_section([(0, 0), (10, 10)]) + walls,
            )
        )
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert metadata["map_width"] == WIDTH
        assert metadata["map_height"] == HEIGHT
        assert metadata["rooms"] == ["Kitchen"]
        assert metadata["virtual_walls"] == 1
        assert metadata["path_points"] == 2

    def test_metadata_never_carries_the_map_body(self):
        # The camera publishes this dict as state attributes; it must stay a summary.
        pytest.importorskip("PIL", reason="Pillow ships with Home Assistant")
        _, metadata = tuya_map.render_tuya_map_png(_map())
        assert all(not isinstance(value, bytes) for value in metadata.values())

    def test_a_zero_resolution_map_does_not_divide_by_zero(self):
        header = bytearray(_map(trailer=_path_section([(1, 1)]))[:24])
        struct.pack_into(">H", header, 12, 0)  # resolution_cm
        raw = bytes(header) + _map(trailer=_path_section([(1, 1)]))[24:]
        with pytest.raises(ValueError):
            tuya_map.render_tuya_map_png(raw)
