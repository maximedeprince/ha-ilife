"""Decode and render Tuya laser-vacuum V2 map files used by ILIFE V20."""
from __future__ import annotations

import io
import struct
from collections import Counter
from typing import Any

from .tuya_lz4 import LZ4BlockError, decompress_block

ROOM_COLORS = {
    0: (249, 66, 79, 255),
    1: (253, 208, 43, 255),
    2: (70, 168, 144, 255),
    3: (32, 140, 255, 255),
    4: (190, 120, 157, 255),
    5: (168, 231, 114, 255),
    6: (73, 249, 202, 255),
    7: (124, 248, 255, 255),
}
C_BACKGROUND = (246, 247, 248, 255)
C_FREE = (216, 222, 232, 255)
C_WALL = (86, 89, 96, 255)
C_CARPET = (197, 184, 232, 255)
C_UNKNOWN = (236, 239, 243, 255)
C_PATH = (255, 255, 255, 235)
C_NO_GO = (59, 130, 246, 72)
C_NO_GO_EDGE = (37, 99, 235, 190)
C_VIRTUAL_WALL = (239, 68, 68, 255)
C_DOCK = (34, 197, 94, 255)
C_ROBOT = (59, 130, 246, 255)


def _signed16(value: int) -> int:
    return value - 65536 if value > 32767 else value


def _parse_header(raw: bytes) -> dict[str, int]:
    if len(raw) < 24:
        raise ValueError("Tuya map is shorter than its 24-byte header")
    fields = struct.unpack(">10H", raw[4:24])
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


def _parse_rooms(data: bytes) -> list[dict[str, Any]]:
    if len(data) < 2:
        return []
    room_count = data[1]
    offset = 2
    rooms = []
    for _ in range(room_count):
        if offset + 47 > len(data):
            raise ValueError("Tuya map has truncated room metadata")
        block = data[offset : offset + 47]
        room_id, order, sweep_count, mop_count = struct.unpack(">4H", block[:8])
        name_len = min(block[26], 19)
        name = block[27 : 27 + name_len].decode("utf-8", errors="replace")
        vertex_count = block[46]
        offset += 47
        vertex_bytes = vertex_count * 4
        if offset + vertex_bytes > len(data):
            raise ValueError("Tuya map has truncated room vertices")
        vertices = [
            list(struct.unpack(">hh", data[start : start + 4]))
            for start in range(offset, offset + vertex_bytes, 4)
        ]
        offset += vertex_bytes
        rooms.append({
            "room_id": room_id,
            "name": name or f"room{room_id}",
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


def _parse_path(data: bytes) -> tuple[list[list[int]], int]:
    """Return path points and the byte offset immediately after the path."""
    if len(data) < 13:
        return [], 0
    point_count = int.from_bytes(data[5:9], "big")
    compressed_length = int.from_bytes(data[11:13], "big")
    if point_count > 2_000_000:
        raise ValueError(f"implausible Tuya path point count: {point_count}")
    if compressed_length:
        end = 13 + compressed_length
        if end > len(data):
            raise ValueError("Tuya map has truncated compressed path data")
        point_bytes = decompress_block(data[13:end], point_count * 4)
    else:
        end = 13 + point_count * 4
        if end > len(data):
            raise ValueError("Tuya map has truncated path data")
        point_bytes = data[13:end]
    if len(point_bytes) != point_count * 4:
        raise ValueError("Tuya path decompressed to an unexpected size")
    points = [
        list(struct.unpack(">hh", point_bytes[start : start + 4]))
        for start in range(0, len(point_bytes), 4)
    ]
    return points, end


def _parse_aa_frames(data: bytes, offset: int) -> list[dict[str, Any]]:
    frames = []
    while offset < len(data):
        if data[offset] != 0xAA:
            offset += 1
            continue
        if offset + 4 > len(data):
            break
        data_length = int.from_bytes(data[offset + 1 : offset + 3], "big")
        end = offset + 3 + data_length + 1
        if end > len(data):
            break
        frame_data = data[offset + 3 : end - 1]
        if not frame_data:
            offset += 1
            continue
        frames.append({
            "command": frame_data[0],
            "payload": bytes(frame_data[1:]),
            "checksum_valid": data[end - 1] == (sum(frame_data) & 0xFF),
        })
        offset = end
    return frames


def _parse_virtual_walls(payload: bytes) -> list[list[list[int]]]:
    if not payload:
        return []
    count = payload[0]
    if len(payload) != 1 + count * 8:
        return []
    return [
        [
            list(struct.unpack(">hh", payload[start : start + 4])),
            list(struct.unpack(">hh", payload[start + 4 : start + 8])),
        ]
        for start in range(1, len(payload), 8)
    ]


def _parse_no_go_zones(payload: bytes) -> list[dict[str, Any]]:
    if not payload:
        return []
    count = payload[0]
    offset = 1
    zones = []
    for index in range(count):
        if offset + 2 > len(payload):
            return []
        zone_type, point_count = payload[offset : offset + 2]
        offset += 2
        if offset + point_count * 4 > len(payload):
            return []
        points = [
            list(struct.unpack(">hh", payload[start : start + 4]))
            for start in range(offset, offset + point_count * 4, 4)
        ]
        offset += point_count * 4
        zones.append({"index": index + 1, "type": zone_type, "points": points})
    return zones if offset == len(payload) else []


def _robot_to_grid(point: list[int], header: dict[str, int]) -> tuple[float, float]:
    units_per_cell = header["resolution_cm"] * 2
    if not units_per_cell:
        raise ValueError("Tuya map has zero resolution")
    return (
        (point[0] + header["origin_x"]) / units_per_cell,
        (-point[1] + header["origin_y"]) / units_per_cell,
    )


def _dashed_line(draw, start, end, fill, width, dash=18, gap=10) -> None:
    import math

    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if not length:
        return
    position = 0.0
    while position < length:
        segment_end = min(position + dash, length)
        p1 = (start[0] + dx * position / length, start[1] + dy * position / length)
        p2 = (
            start[0] + dx * segment_end / length,
            start[1] + dy * segment_end / length,
        )
        draw.line([p1, p2], fill=fill, width=width)
        position += dash + gap


def decode_tuya_map(layout: bytes, path_file: bytes | None = None) -> dict[str, Any]:
    """Decode one Tuya V2 layout and optional separate path file."""
    header = _parse_header(layout)
    if header["version"] != 2:
        raise NotImplementedError(
            f"Tuya map version {header['version']} is not supported by the V20 decoder"
        )
    compressed_start = 24
    compressed_end = compressed_start + header["compressed_length"]
    if compressed_end > len(layout):
        raise ValueError("Tuya map has truncated compressed layout data")
    decoded = decompress_block(
        layout[compressed_start:compressed_end], header["decompressed_length"]
    )
    area = header["width"] * header["height"]
    if len(decoded) < area:
        raise ValueError("Tuya map decompressed to fewer bytes than the declared grid")
    grid = decoded[:area]
    rooms = _parse_rooms(decoded[area:])

    embedded = layout[compressed_end:]
    embedded_points, frame_offset = _parse_path(embedded)
    frames = _parse_aa_frames(embedded, frame_offset)
    path_points = embedded_points
    if path_file:
        try:
            separate_points, _ = _parse_path(path_file)
        except (ValueError, LZ4BlockError):
            separate_points = []
        if separate_points:
            path_points = separate_points

    walls = []
    zones = []
    valid_commands = []
    for frame in frames:
        if not frame["checksum_valid"]:
            continue
        valid_commands.append(f"0x{frame['command']:02X}")
        if frame["command"] == 0x13:
            walls = _parse_virtual_walls(frame["payload"])
        elif frame["command"] == 0x1B:
            zones = _parse_no_go_zones(frame["payload"])

    counts = Counter(grid)
    room_areas = {
        room["room_id"]: round(
            counts.get((room["room_id"] << 3) | 0x07, 0)
            * header["resolution_cm"] ** 2
            / 10_000,
            2,
        )
        for room in rooms
    }
    return {
        "header": header,
        "grid": grid,
        "rooms": rooms,
        "room_areas_m2": room_areas,
        "path_points": path_points,
        "virtual_walls": walls,
        "no_go_zones": zones,
        "embedded_commands": valid_commands,
    }


def render_tuya_map_png(
    layout: bytes, path_file: bytes | None = None, scale: int = 6
) -> tuple[bytes, dict[str, Any]]:
    """Render a Tuya V2 map to PNG and return compact camera metadata."""
    from PIL import Image, ImageDraw, ImageFont

    decoded = decode_tuya_map(layout, path_file)
    header = decoded["header"]
    width, height = header["width"], header["height"]
    room_ids = {room["room_id"] for room in decoded["rooms"]}

    pixels = []
    for value in decoded["grid"]:
        room_id, pixel_type = value >> 3, value & 0x07
        if pixel_type == 0x01:
            color = C_WALL
        elif pixel_type == 0x02:
            color = C_CARPET
        elif pixel_type == 0x07 and room_id in room_ids:
            color = ROOM_COLORS.get(room_id, C_FREE)
        elif pixel_type == 0x07:
            color = C_FREE
        elif pixel_type == 0x00:
            color = C_BACKGROUND
        else:
            color = C_UNKNOWN
        pixels.append(color)

    image = Image.new("RGBA", (width, height))
    image.putdata(pixels)
    image = image.resize((width * scale, height * scale), Image.Resampling.NEAREST)

    def canvas_point(point: list[int]) -> tuple[float, float]:
        x, y = _robot_to_grid(point, header)
        return x * scale, y * scale

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    for zone in decoded["no_go_zones"]:
        polygon = [canvas_point(point) for point in zone["points"]]
        overlay_draw.polygon(polygon, fill=C_NO_GO)
        overlay_draw.line(
            polygon + [polygon[0]], fill=C_NO_GO_EDGE, width=max(1, scale // 3)
        )
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image, "RGBA")

    path_xy = [canvas_point(point) for point in decoded["path_points"]]
    if len(path_xy) > 1:
        segment = [path_xy[0]]
        for previous, current in zip(path_xy, path_xy[1:]):
            distance = ((current[0] - previous[0]) ** 2 + (current[1] - previous[1]) ** 2) ** 0.5
            if distance > 8 * scale:
                if len(segment) > 1:
                    draw.line(segment, fill=C_PATH, width=max(2, scale // 2), joint="curve")
                segment = [current]
            else:
                segment.append(current)
        if len(segment) > 1:
            draw.line(segment, fill=C_PATH, width=max(2, scale // 2), joint="curve")

    for wall in decoded["virtual_walls"]:
        _dashed_line(
            draw,
            canvas_point(wall[0]),
            canvas_point(wall[1]),
            C_VIRTUAL_WALL,
            max(2, scale // 2),
        )

    dock = (
        header["pile_x"] / (header["resolution_cm"] * 2) * scale,
        header["pile_y"] / (header["resolution_cm"] * 2) * scale,
    )

    def marker(center, color, radius):
        x, y = center
        draw.ellipse(
            [x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2],
            fill=(255, 255, 255, 245),
        )
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)

    marker(dock, C_DOCK, max(5, scale))
    if path_xy:
        marker(path_xy[-1], C_ROBOT, max(4, scale - 1))

    try:
        font = ImageFont.load_default(size=max(12, scale * 2))
    except TypeError:
        font = ImageFont.load_default()
    grid_width = width
    for room in decoded["rooms"]:
        room_value = (room["room_id"] << 3) | 0x07
        positions = [
            (index % grid_width, index // grid_width)
            for index, value in enumerate(decoded["grid"])
            if value == room_value
        ]
        if not positions:
            continue
        x = sum(point[0] for point in positions) / len(positions) * scale
        y = sum(point[1] for point in positions) / len(positions) * scale
        text = room["name"]
        box = draw.textbbox((x, y), text, font=font, anchor="mm")
        pad = max(3, scale // 2)
        draw.rounded_rectangle(
            [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad],
            radius=pad,
            fill=(255, 255, 255, 190),
        )
        draw.text((x, y), text, fill=(31, 41, 55, 255), font=font, anchor="mm")

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    metadata = {
        "map_version": header["version"],
        "map_id": header["map_id"],
        "map_width": width,
        "map_height": height,
        "resolution_cm": header["resolution_cm"],
        "rooms": [room["name"] for room in decoded["rooms"]],
        "room_areas_m2": decoded["room_areas_m2"],
        "path_points": len(decoded["path_points"]),
        "virtual_walls": len(decoded["virtual_walls"]),
        "no_go_zones": len(decoded["no_go_zones"]),
    }
    return output.getvalue(), metadata
