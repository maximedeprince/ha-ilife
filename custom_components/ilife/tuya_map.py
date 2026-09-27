"""Decode and render the Tuya laser-vacuum map files of ILIFE Clean models.

Two map versions are known, and they share everything but two details. All
integers are big-endian.

    header    24 bytes   version (u8), map id (u16), type (u8), then ten u16:
                         width, height, origin x, origin y (signed), resolution
                         in cm, dock x, dock y (signed), reserved, decompressed
                         length, compressed length
    grid      LZ4 block  width * height cells, row 0 at the top, followed by the
                         room metadata (see `_parse_rooms`)
    path      13 bytes   preamble with the point count and compressed length, then
                         the (LZ4) s16 x/y points; see `_parse_path`
    overlays  after the path: virtual walls and no-go zones

What differs:

    version 2 (V20)      cells `(room_id << 3) | type` (1 wall, 2 carpet, 7 floor,
                         0 outside); overlays in checksummed 0xAA frames, points
                         in robot units
    version 1 (A30 Pro)  cells `(room_id << 2) | flags`, 0xF9 wall, 0xFF outside;
                         overlays in plain tagged records, points in *cells*
                         from the robot origin (worked out against the app's own
                         rendering of the same map), then a trailer not read

The decoder turns both into one shape: cells as (kind, room id), overlays in
robot units. Three coordinate systems are in play:

    robot units  path points. `resolution_cm * 2` units per cell, x to the right,
                 y *up*, zero at the header's origin.
    cell offsets A30 Pro walls and zones: whole cells from the same origin, also
                 y up — robot units divided by the units per cell.
    grid units   the dock position. Cells * units per cell from the top-left
                 corner, y down, like the grid itself.
"""
from __future__ import annotations

import base64
import io
import struct
from collections import Counter
from typing import Any

from .tuya_lz4 import LZ4BlockError, decompress_block

VERSION_A30 = 1
VERSION_V20 = 2
SUPPORTED_MAP_VERSIONS = {VERSION_A30: "A30 Pro", VERSION_V20: "V20"}

HEADER_LENGTH = 24
ROOM_RECORD_LENGTH = 47
PATH_PREAMBLE_LENGTH = 13
MAX_PATH_POINTS = 2_000_000

# What a cell is, whatever the version encodes it as.
KIND_OUTSIDE, KIND_FLOOR, KIND_WALL, KIND_CARPET, KIND_UNKNOWN = range(5)

# Version 1 cells: anything else is a room cell, `(room_id << 2) | flags`.
CELL_OUTSIDE = 0xFF
CELL_OBSTACLE = 0xF9

# Overlay tags: version 1 record tags, and version 2 frame commands.
TAG_VIRTUAL_WALLS = 0x13
TAG_NO_GO_ZONES = 0x1B

# 0x86D2, 0x86D3: sits between path points the robot travelled without cleaning
# (the run back to the dock). It is a marker, not a position.
PATH_BREAK = [-31022, -31021]

ROOM_COLORS = (
    (249, 66, 79, 255),
    (253, 208, 43, 255),
    (70, 168, 144, 255),
    (32, 140, 255, 255),
    (190, 120, 157, 255),
    (168, 231, 114, 255),
    (73, 249, 202, 255),
    (124, 248, 255, 255),
)
C_OUTSIDE = (216, 222, 232, 255)
C_FLOOR = (184, 196, 214, 255)
C_WALL = (86, 89, 96, 255)
C_CARPET = (197, 184, 232, 255)
C_UNKNOWN = (236, 239, 243, 255)
C_PATH = (255, 255, 255, 235)
C_TRAVEL = (255, 255, 255, 110)
# Light fill + red edge: stays visible over every room colour, red rooms included.
C_NO_GO = (255, 255, 255, 110)
C_NO_GO_EDGE = (220, 38, 38, 230)
C_VIRTUAL_WALL = (239, 68, 68, 255)
C_DOCK = (34, 197, 94, 255)
C_ROBOT = (59, 130, 246, 255)
C_LABEL = (31, 41, 55, 255)
C_HALO = (255, 255, 255, 245)


def _signed16(value: int) -> int:
    return value - 65536 if value > 32767 else value


def _points(data: bytes, start: int, count: int) -> list[list[int]]:
    """Read `count` s16 x/y pairs starting at `start`; the caller checks the bounds."""
    return [
        list(struct.unpack_from(">hh", data, start + index * 4))
        for index in range(count)
    ]


def _parse_header(raw: bytes) -> dict[str, int]:
    if len(raw) < HEADER_LENGTH:
        raise ValueError("Tuya map is shorter than its 24-byte header")
    fields = struct.unpack(">10H", raw[4:HEADER_LENGTH])
    return {
        "version": raw[0],
        "map_id": int.from_bytes(raw[1:3], "big"),
        "type": raw[3],
        "width": fields[0],
        "height": fields[1],
        "origin_x": _signed16(fields[2]),
        "origin_y": _signed16(fields[3]),
        "resolution_cm": fields[4],
        "pile_x": _signed16(fields[5]),
        "pile_y": _signed16(fields[6]),
        "decompressed_length": fields[8],
        "compressed_length": fields[9],
    }


def _classify_v1(value: int) -> tuple[int, int | None]:
    if value == CELL_OUTSIDE:
        return KIND_OUTSIDE, None
    if value == CELL_OBSTACLE:
        return KIND_WALL, None
    return KIND_FLOOR, value >> 2


def _classify_v2(value: int) -> tuple[int, int | None]:
    room_id, cell_type = value >> 3, value & 0x07
    if cell_type == 0x07:
        return KIND_FLOOR, room_id
    return {0x00: KIND_OUTSIDE, 0x01: KIND_WALL, 0x02: KIND_CARPET}.get(
        cell_type, KIND_UNKNOWN), None


# (kind, room id) for every byte value, per version.
_CELLS = {
    VERSION_A30: [_classify_v1(value) for value in range(256)],
    VERSION_V20: [_classify_v2(value) for value in range(256)],
}


def _parse_rooms(data: bytes) -> list[dict[str, Any]]:
    """Room metadata: a count at byte 1, then one 47-byte record + vertices per room.

    The A30 Pro leaves names empty and sends no vertices; room N then reads as
    "Room N", as the app numbers them.
    """
    if len(data) < 2:
        return []
    room_count = data[1]
    offset = 2
    rooms = []
    for _ in range(room_count):
        if offset + ROOM_RECORD_LENGTH > len(data):
            raise ValueError("Tuya map has truncated room metadata")
        block = data[offset : offset + ROOM_RECORD_LENGTH]
        room_id, order, sweep_count, mop_count = struct.unpack(">4H", block[:8])
        name_len = min(block[26], 19)
        name = block[27 : 27 + name_len].decode("utf-8", errors="replace")
        if not name.isprintable():
            name = ""
        vertex_count = block[46]
        offset += ROOM_RECORD_LENGTH
        if offset + vertex_count * 4 > len(data):
            raise ValueError("Tuya map has truncated room vertices")
        vertices = _points(data, offset, vertex_count)
        offset += vertex_count * 4
        rooms.append({
            "room_id": room_id,
            "name": name or f"Room {room_id + 1}",
            "order": order,
            "sweep_count": sweep_count,
            "mop_count": mop_count,
            "color_order": block[8],
            "sweep_forbidden": block[9],
            "mop_forbidden": block[10],
            "fan": block[11],
            "water": block[12],
            "y_mode": block[13],
            "vertices": vertices,
        })
    return rooms


def _parse_path(data: bytes) -> tuple[list[list[int]], list[bool], int]:
    """Return the path points, whether each was reached travelling (not cleaning),
    and the offset after the path. Break markers are dropped: a point right after
    one is a travel point.

    Preamble: point count at bytes 5-8, compressed length at bytes 11-12 (0 when
    the points follow uncompressed).
    """
    if len(data) < PATH_PREAMBLE_LENGTH:
        return [], [], len(data)
    point_count = int.from_bytes(data[5:9], "big")
    compressed_length = int.from_bytes(data[11:13], "big")
    if point_count > MAX_PATH_POINTS:
        raise ValueError(f"implausible Tuya path point count: {point_count}")
    start = PATH_PREAMBLE_LENGTH
    if compressed_length:
        end = start + compressed_length
        if end > len(data):
            raise ValueError("Tuya map has truncated compressed path data")
        point_bytes = decompress_block(data[start:end], point_count * 4)
    else:
        end = start + point_count * 4
        if end > len(data):
            raise ValueError("Tuya map has truncated path data")
        point_bytes = data[start:end]
    if len(point_bytes) != point_count * 4:
        raise ValueError("Tuya path decompressed to an unexpected size")
    points, travel, after_break = [], [], False
    for point in _points(point_bytes, 0, point_count):
        if point == PATH_BREAK:
            after_break = True
            continue
        points.append(point)
        travel.append(after_break)
        after_break = False
    return points, travel, end


def _parse_records(
    data: bytes, offset: int
) -> tuple[list[list[list[int]]], list[list[list[int]]]]:
    """Version 1 overlays: tagged records right after the path.

    0x13: count (u8), then per wall two points.
    0x1B: count (u16), then per zone four corner points.
    Points are cell offsets (see the module docstring). Parsing stops at the first
    tag it does not know: nothing after it can be located reliably.
    """
    walls: list[list[list[int]]] = []
    zones: list[list[list[int]]] = []
    while offset < len(data):
        tag = data[offset]
        if tag == TAG_VIRTUAL_WALLS:
            if offset + 2 > len(data):
                raise ValueError("Tuya map has a truncated virtual-wall record")
            count = data[offset + 1]
            start, per_item, points_per_item = offset + 2, 8, 2
        elif tag == TAG_NO_GO_ZONES:
            if offset + 3 > len(data):
                raise ValueError("Tuya map has a truncated no-go-zone record")
            count = int.from_bytes(data[offset + 1 : offset + 3], "big")
            start, per_item, points_per_item = offset + 3, 16, 4
        else:
            break
        end = start + count * per_item
        if end > len(data):
            raise ValueError(f"Tuya map record 0x{tag:02X} runs past the end of the file")
        items = [
            _points(data, start + index * per_item, points_per_item)
            for index in range(count)
        ]
        (walls if tag == TAG_VIRTUAL_WALLS else zones).extend(items)
        offset = end
    return walls, zones


def _parse_frames(
    data: bytes, offset: int
) -> tuple[list[list[list[int]]], list[list[list[int]]]]:
    """Version 2 overlays: 0xAA frames (length u16, command, payload, sum checksum)
    after the path; frames with a bad checksum are skipped. Points are robot units.

    0x13: count (u8), then per wall two points.
    0x1B: count (u8), then per zone a type (u8), a point count (u8) and the points.
    """
    walls: list[list[list[int]]] = []
    zones: list[list[list[int]]] = []
    while offset < len(data):
        if data[offset] != 0xAA:
            offset += 1
            continue
        if offset + 4 > len(data):
            break
        end = offset + 3 + int.from_bytes(data[offset + 1 : offset + 3], "big") + 1
        if end > len(data):
            break
        body = data[offset + 3 : end - 1]
        if not body:
            offset += 1
            continue
        if data[end - 1] != sum(body) & 0xFF:
            offset = end
            continue
        command, payload = body[0], body[1:]
        if command == TAG_VIRTUAL_WALLS and payload \
                and len(payload) == 1 + payload[0] * 8:
            walls = [_points(payload, 1 + index * 8, 2) for index in range(payload[0])]
        elif command == TAG_NO_GO_ZONES and payload:
            parsed, cursor = [], 1
            for _ in range(payload[0]):
                if cursor + 2 > len(payload):
                    break
                point_count = payload[cursor + 1]
                cursor += 2
                if cursor + point_count * 4 > len(payload):
                    break
                parsed.append(_points(payload, cursor, point_count))
                cursor += point_count * 4
            if cursor == len(payload):
                zones = parsed
        offset = end
    return walls, zones


def _units_per_cell(header: dict[str, int]) -> int:
    units = header["resolution_cm"] * 2
    if not units:
        raise ValueError("Tuya map has zero resolution")
    return units


def _robot_to_cell(point: list[int], header: dict[str, int]) -> tuple[float, float]:
    """Robot units (y up) -> grid cell (y down)."""
    units = _units_per_cell(header)
    return (
        (header["origin_x"] + point[0]) / units,
        (header["origin_y"] - point[1]) / units,
    )


def _dashed_line(draw, start, end, fill, width, dash=18, gap=10) -> None:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if not length:
        return
    position = 0.0
    while position < length:
        segment_end = min(position + dash, length)
        draw.line(
            [
                (start[0] + dx * position / length, start[1] + dy * position / length),
                (start[0] + dx * segment_end / length, start[1] + dy * segment_end / length),
            ],
            fill=fill,
            width=width,
        )
        position += dash + gap


# Fonts with wide Latin coverage, tried before Pillow's built-in one, which lacks
# letters such as "Ł" and draws a box for them.
LABEL_FONTS = ("DejaVuSans.ttf", "NotoSans-Regular.ttf", "LiberationSans-Regular.ttf",
               "Arial.ttf")
_FOLD = str.maketrans({"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Ø": "O", "ø": "o",
                       "Ħ": "H", "ħ": "h", "ı": "i", "Œ": "OE", "œ": "oe"})


def _label_font(size: int):
    """(font, whether it covers more than Latin-1)."""
    from PIL import ImageFont

    for name in LABEL_FONTS:
        try:
            return ImageFont.truetype(name, size), True
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size), False
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default(), False


def _fold_to_latin1(text: str) -> str:
    """Strip what the built-in font cannot draw: "Łazienka" -> "Lazienka"."""
    import unicodedata

    out = []
    for char in text.translate(_FOLD):
        if ord(char) < 256:
            out.append(char)
            continue
        base = unicodedata.normalize("NFKD", char).encode("latin-1", "ignore").decode("latin-1")
        out.append(base or "?")
    return "".join(out)


def decode_tuya_map(
    layout: bytes, path_file: bytes | None = None, *, include_path: bool = True
) -> dict[str, Any]:
    """Decode one layout file, of either known version.

    `path_file` is a separate path file in the same format as the embedded path;
    when it parses, its points replace the embedded ones. `include_path=False`
    drops the path altogether — for showing a stored layout without the previous
    run's path on it.

    `cells` holds (kind, room id or None) per grid cell; `virtual_walls` and
    `no_go_zones` are point lists in robot units, whatever the file stored.
    """
    header = _parse_header(layout)
    version = header["version"]
    if version not in SUPPORTED_MAP_VERSIONS:
        raise NotImplementedError(
            f"Tuya map version {version} is not supported (known: "
            + ", ".join(f"{v} ({model})" for v, model in SUPPORTED_MAP_VERSIONS.items())
            + ")"
        )
    units = _units_per_cell(header)
    compressed_end = HEADER_LENGTH + header["compressed_length"]
    if compressed_end > len(layout):
        raise ValueError("Tuya map has truncated compressed layout data")
    decoded = decompress_block(
        layout[HEADER_LENGTH:compressed_end], header["decompressed_length"]
    )
    area = header["width"] * header["height"]
    if len(decoded) < area:
        raise ValueError("Tuya map decompressed to fewer bytes than the declared grid")
    grid = decoded[:area]
    rooms = _parse_rooms(decoded[area:])
    table = _CELLS[version]
    cells = [table[value] for value in grid]

    trailer = layout[compressed_end:]
    path_points, path_travel, overlay_offset = _parse_path(trailer)
    if version == VERSION_A30:
        walls, zones = _parse_records(trailer, overlay_offset)
        walls = [[[x * units, y * units] for x, y in wall] for wall in walls]
        zones = [[[x * units, y * units] for x, y in zone] for zone in zones]
    else:
        walls, zones = _parse_frames(trailer, overlay_offset)
    if path_file:
        try:
            separate_points, separate_travel, _ = _parse_path(path_file)
        except (ValueError, LZ4BlockError):
            separate_points, separate_travel = [], []
        if separate_points:
            path_points, path_travel = separate_points, separate_travel
    if not include_path:
        path_points, path_travel = [], []

    cell_counts = Counter(room for kind, room in cells if kind == KIND_FLOOR)
    cell_area_m2 = header["resolution_cm"] ** 2 / 10_000
    room_areas = {
        room["room_id"]: round(cell_counts[room["room_id"]] * cell_area_m2, 2)
        for room in rooms
    }
    return {
        "header": header,
        "grid": grid,
        "cells": cells,
        "rooms": rooms,
        "room_areas_m2": room_areas,
        "path_points": path_points,
        "path_travel": path_travel,
        "virtual_walls": walls,
        "no_go_zones": zones,
    }


def clean_map_thumbnail(layout: bytes, factor: int = 2) -> str:
    """The layout as the card's history thumbnail format (ILIFEHOME's CleanMapData):
    01, bytes per row, then 2 bits per cell (1 floor, 2 wall), 4 cells a byte,
    first cell in the high bits. Downscaled by `factor` to keep the attribute small.
    """
    decoded = decode_tuya_map(layout, include_path=False)
    width, height = decoded["header"]["width"], decoded["header"]["height"]
    cells = decoded["cells"]
    cols, rows = -(-width // factor), -(-height // factor)
    bytes_per_row = -(-cols // 4)
    if bytes_per_row > 255:
        raise ValueError("Tuya map is too wide for a thumbnail")
    out = bytearray([0x01, bytes_per_row])
    for row in range(rows):
        line = bytearray(bytes_per_row)
        for col in range(cols):
            value = 0
            for y in range(row * factor, min(height, (row + 1) * factor)):
                for x in range(col * factor, min(width, (col + 1) * factor)):
                    kind = cells[y * width + x][0]
                    if kind == KIND_WALL:
                        value = 2
                    elif kind in (KIND_FLOOR, KIND_CARPET) and not value:
                        value = 1
            line[col // 4] |= value << ((3 - col % 4) * 2)
        out += line
    return base64.b64encode(bytes(out)).decode()


def clean_map_path(layout: bytes, factor: int = 2) -> list[list[int]]:
    """The cleaning runs of a layout's path in `clean_map_thumbnail`'s cells, for
    the card to draw over the thumbnail: [[x0, y0, x1, y1, ...], ...]. Travel legs
    and points off the grid split the runs and are left out."""
    decoded = decode_tuya_map(layout)
    header = decoded["header"]
    width, height = header["width"], header["height"]
    runs, run = [], []
    for point, travelled in zip(decoded["path_points"], decoded["path_travel"]):
        cell_x, cell_y = _robot_to_cell(point, header)
        if travelled or not (0 <= cell_x < width and 0 <= cell_y < height):
            if len(run) > 2:
                runs.append(run)
            run = []
            if travelled and 0 <= cell_x < width and 0 <= cell_y < height:
                run = [int(cell_x // factor), int(cell_y // factor)]
            continue
        run += [int(cell_x // factor), int(cell_y // factor)]
    if len(run) > 2:
        runs.append(run)
    return runs


def render_tuya_map_png(
    layout: bytes,
    path_file: bytes | None = None,
    scale: int = 6,
    *,
    include_path: bool = True,
    room_names: dict[int, str] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Render a map to PNG and return compact camera metadata (never the map body).

    `room_names` ({room id: name}) replaces the names in the file, which the A30
    Pro leaves empty; the app keeps them in separate DPs (see tuya_rooms).
    """
    from PIL import Image, ImageDraw

    decoded = decode_tuya_map(layout, path_file, include_path=include_path)
    for room in decoded["rooms"]:
        room["name"] = (room_names or {}).get(room["room_id"], room["name"])
    header = decoded["header"]
    width, height = header["width"], header["height"]
    room_ids = {room["room_id"] for room in decoded["rooms"]}

    # One pass over the grid: colour each cell and gather each room's centroid.
    kind_colors = {KIND_OUTSIDE: C_OUTSIDE, KIND_FLOOR: C_FLOOR, KIND_WALL: C_WALL,
                   KIND_CARPET: C_CARPET, KIND_UNKNOWN: C_UNKNOWN}
    pixels = []
    sums: dict[int, list[int]] = {room_id: [0, 0, 0] for room_id in room_ids}
    for index, (kind, room_id) in enumerate(decoded["cells"]):
        if kind != KIND_FLOOR or room_id not in room_ids:
            pixels.append(kind_colors[kind])
            continue
        pixels.append(ROOM_COLORS[room_id % len(ROOM_COLORS)])
        total = sums[room_id]
        total[0] += index % width
        total[1] += index // width
        total[2] += 1

    image = Image.new("RGBA", (width, height))
    image.putdata(pixels)
    image = image.resize((width * scale, height * scale), Image.Resampling.NEAREST)

    def canvas(cell: tuple[float, float]) -> tuple[float, float]:
        # Coordinates name a cell; draw at its centre.
        return (cell[0] + 0.5) * scale, (cell[1] + 0.5) * scale

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    for zone in decoded["no_go_zones"]:
        polygon = [canvas(_robot_to_cell(point, header)) for point in zone]
        overlay_draw.polygon(polygon, fill=C_NO_GO)
        overlay_draw.line(polygon + [polygon[0]], fill=C_NO_GO_EDGE, width=max(2, scale // 3))
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image, "RGBA")

    # Cleaning runs are drawn solid; the legs the robot only travelled (to a
    # room, back to the dock) faint and dashed, so the map shows what was
    # cleaned. A point outside the grid is not a position the robot was at (the
    # A30 Pro mixes a few into its stored path): it breaks the line instead of
    # drawing one across the whole image to it.
    cleaning_runs, run, previous, off_map, robot = [], [], None, 0, None
    for point, travelled in zip(decoded["path_points"], decoded["path_travel"]):
        cell_x, cell_y = _robot_to_cell(point, header)
        if not (0 <= cell_x < width and 0 <= cell_y < height):
            off_map += 1
            previous = None
            continue
        xy = canvas((cell_x, cell_y))
        if travelled or previous is None:
            if len(run) > 1:
                cleaning_runs.append(run)
            run = [xy]
            if travelled and previous is not None:
                _dashed_line(draw, previous, xy, C_TRAVEL, max(1, scale // 4),
                             dash=scale, gap=scale)
        else:
            run.append(xy)
        previous = robot = xy
    if len(run) > 1:
        cleaning_runs.append(run)
    for part in cleaning_runs:
        draw.line(part, fill=C_PATH, width=max(2, scale // 2), joint="curve")

    for start, end in decoded["virtual_walls"]:
        _dashed_line(draw, canvas(_robot_to_cell(start, header)),
                     canvas(_robot_to_cell(end, header)),
                     C_VIRTUAL_WALL, max(2, scale // 2))

    def marker(center, color, radius):
        x, y = center
        draw.ellipse([x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2],
                     fill=C_HALO)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)

    units = _units_per_cell(header)
    marker(canvas((header["pile_x"] / units, header["pile_y"] / units)),
           C_DOCK, max(5, scale))
    if robot is not None:
        marker(robot, C_ROBOT, max(4, scale - 1))

    font, full_unicode = _label_font(max(12, scale * 2))
    pad = max(3, scale // 2)
    for room in decoded["rooms"]:
        sum_x, sum_y, count = sums[room["room_id"]]
        if not count:
            continue
        center = canvas((sum_x / count, sum_y / count))
        label = room["name"] if full_unicode else _fold_to_latin1(room["name"])
        box = draw.textbbox(center, label, font=font, anchor="mm")
        draw.rounded_rectangle(
            [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad],
            radius=pad,
            fill=(255, 255, 255, 190),
        )
        draw.text(center, label, fill=C_LABEL, font=font, anchor="mm")

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    metadata = {
        "map_version": header["version"],
        "map_id": header["map_id"],
        "map_width": width,
        "map_height": height,
        "resolution_cm": header["resolution_cm"],
        "rooms": {room["room_id"]: room["name"] for room in decoded["rooms"]},
        "room_areas_m2": decoded["room_areas_m2"],
        "path_points": len(decoded["path_points"]),
        "path_points_off_map": off_map,
        "virtual_walls": len(decoded["virtual_walls"]),
        "no_go_zones": len(decoded["no_go_zones"]),
    }
    return output.getvalue(), metadata
