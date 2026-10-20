"""Saved maps, rooms and live map for SLAM ("X9" family) models — e.g. AVA PRO MAX.

Reverse-engineered from the app (DataUtils.parseSaveMapDataX9, HomeX9ViewModel.parseRoomInfo,
RoomHelper) and checked against a real AVA PRO MAX state dump.

Grid maps (SaveMapDataX9_n.MapData1..7, RealMapData_n.MapData1..8): base64 chunks that
concatenate to one byte stream:
    [0] u8 type (1 = saved, 0 = live)  [1] u8 resolution (mm per cell)
    [2:4] i16 BE A  [4:6] i16 BE B  [6:8] i16 BE W (row width)
    [8:]  run-length pairs (value u8, count u8), row-major, W cells per row.
  Cell (row, col) is the map point x = A - row, y = col - B — the same frame as the robot /
  charger positions and the zone / go-to commands.
  Values: 1 wall, 2 floor (no room), 3 unknown (skipped), 5 path, 11.. room floor where
  room bit = 1 << (value - 11).
  A map split in several packs: PackId = (index << 16) | count; every pack after the first
  repeats the 8-byte header, which is dropped before concatenating.

Room names (MapRoomInfo1..10): base64 of "mapId,mapName,bit,name,bit,name,...".
Active map: SaveMap.SelectedMapId (0 = none).
Room clean = CleanPartitionData.PartitionData, the OR of the room bits.

map.decode_realmap reads the same live-map format as raster indices for display; this
module keeps map coordinates, which the zone and go-to commands need.
"""
from __future__ import annotations

import base64
import io
import struct

UNKNOWN = 3
WALL = 1
FLOOR = 2
PATH = 5
ROOM_BASE = 11

ROOM_COLORS = [
    (232, 122, 100), (98, 178, 228), (122, 200, 122), (228, 198, 90), (178, 130, 220),
    (240, 158, 196), (138, 218, 208), (198, 168, 120), (158, 158, 238), (248, 140, 80),
    (128, 198, 158), (210, 120, 160), (120, 160, 200), (190, 200, 110), (160, 110, 110),
    (110, 190, 230),
]
C_WALL = (67, 70, 79, 255)
C_FLOOR = (200, 203, 210, 255)
C_CLEANED = (255, 255, 255, 90)
C_PATH = (255, 255, 255, 220)
C_ROBOT = (231, 76, 60, 255)
C_CHARGER = (46, 204, 113, 255)


def unpack_point(v):
    """Packed position (x << 16) | (y & 0xFFFF), two i16 -> (x, y) or None."""
    if not isinstance(v, int):
        return None
    return struct.unpack(">hh", (v & 0xFFFFFFFF).to_bytes(4, "big"))


def room_bit(value):
    return 1 << (value - ROOM_BASE) if value >= ROOM_BASE else None


def _pack_index(item):
    pid = item.get("PackId") or 0
    return ((pid >> 16) & 0xFFFF) or 1, (pid & 0xFFFF) or 1


def pack_count(item):
    return _pack_index(item or {})[1]


def join_packs(items, prefix, chunks):
    """Ordered packs -> one byte stream (header kept from the first pack only)."""
    out = bytearray()
    for n, item in enumerate(sorted(items, key=lambda it: _pack_index(it)[0])):
        for i in range(1, chunks + 1):
            s = item.get(f"{prefix}{i}")
            if not s:
                continue
            try:
                b = base64.b64decode(s)
            except Exception:  # noqa: BLE001
                continue
            if n and i == 1:
                b = b[8:]
            out += b
    return bytes(out)


def decode_grid(raw):
    """RLE grid bytes -> ({(x, y): value}, resolution_mm). Unknown cells are skipped."""
    if len(raw) < 10:
        return {}, None
    res = raw[1]
    a, b, w = struct.unpack(">hhh", raw[2:8])
    if w <= 0:
        return {}, res
    cells = {}
    row = col = 0
    for i in range(8, len(raw) - 1, 2):
        val, cnt = raw[i], raw[i + 1]
        if val == UNKNOWN:
            row += (col + cnt) // w
            col = (col + cnt) % w
            continue
        for _ in range(cnt):
            cells[(a - row, col - b)] = val
            col += 1
            if col >= w:
                col = 0
                row += 1
    return cells, res


def selected_map_id(state):
    sm = (state or {}).get("SaveMap")
    return (sm or {}).get("SelectedMapId") if isinstance(sm, dict) else None


def saved_map_slot(state, map_id):
    """(property name, value) of the SaveMapDataX9_n slot holding map_id, or (None, None)."""
    if not map_id:
        return None, None
    for n in range(1, 11):
        key = f"SaveMapDataX9_{n}"
        v = (state or {}).get(key)
        if isinstance(v, dict) and v.get("MapId") == map_id:
            return key, v
    return None, None


def room_info(state, map_id):
    """MapRoomInfo for map_id -> (map name, {bit: name}) ordered by bit.
    Names are typed by the user and may contain commas: a token that isn't a room bit
    belongs to the name before it."""
    for n in range(1, 11):
        raw = (state or {}).get(f"MapRoomInfo{n}")
        if not raw:
            continue
        try:
            parts = base64.b64decode(raw).decode("utf-8").split(",")
        except Exception:  # noqa: BLE001
            continue
        if not parts or parts[0] != str(map_id):
            continue
        map_name, names, bit = [], {}, None
        for tok in parts[1:]:
            if tok.isdigit() and _is_bit(int(tok)) and (names or map_name) and \
                    (bit is None or names.get(bit) is not None):
                bit = int(tok)
                names[bit] = None
            elif bit is None:
                map_name.append(tok)
            else:
                names[bit] = tok if names[bit] is None else f"{names[bit]},{tok}"
        names = {b: (v or "") for b, v in names.items()}
        return (",".join(map_name) or None), dict(sorted(names.items()))
    return None, {}


def _is_bit(v):
    return v > 0 and not v & (v - 1)


def build_rooms(cells, names):
    """Rooms of a saved map: [{id, name, x, y, cells}] — (x, y) = centroid in map coordinates.
    Rooms present in the grid but unnamed in the app get a generic name."""
    acc = {}
    for (x, y), v in (cells or {}).items():
        bit = room_bit(v)
        if bit is None:
            continue
        s = acc.setdefault(bit, [0, 0, 0])
        s[0] += x
        s[1] += y
        s[2] += 1
    rooms = []
    for bit in sorted(set(acc) | set(names or {})):
        sx, sy, n = acc.get(bit, (0, 0, 0))
        rooms.append({
            "id": bit,
            "name": (names or {}).get(bit) or f"Room {bit.bit_length()}",
            "x": round(sx / n) if n else None,
            "y": round(sy / n) if n else None,
            "cells": n,
        })
    return rooms


def live_packs(state, session=None):
    """RealMapData_n packs of the newest upload, ignoring one older than the current
    session (RealTimeMapStart): that is the previous clean's map."""
    packs = [v for k, v in (state or {}).items()
             if k.startswith("RealMapData_") and isinstance(v, dict)]
    if not packs:
        return []
    newest = max(p.get("UpdateTime") or 0 for p in packs)
    if isinstance(session, int) and newest and newest < session:
        return []
    packs = [p for p in packs if (p.get("UpdateTime") or 0) == newest]
    count = pack_count(packs[0])
    return [p for p in packs if _pack_index(p)[0] <= count]


def extend_path(path, window):
    """Append a RoadData window to the path, skipping the points it shares with the
    path's end (consecutive windows overlap)."""
    path = list(path)
    k = min(len(path), len(window))
    while k and path[-k:] != list(window[:k]):
        k -= 1
    for p in window[k:]:
        if not path or path[-1] != p:
            path.append(p)
    return path


def decode_points(b64):
    """RoadData: base64 run of (i16 BE x, i16 BE y) points."""
    try:
        b = base64.b64decode(b64 or "")
    except Exception:  # noqa: BLE001
        return []
    return [struct.unpack(">hh", b[i:i + 4]) for i in range(0, len(b) - 3, 4)]


def encode_rect(x1, y1, x2, y2):
    """CleanAreaData.AreaData: 4 corners (i16 BE x, y), in the order the app sends them:
    (xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin)."""
    xa, xb = sorted((int(round(x1)), int(round(x2))))
    ya, yb = sorted((int(round(y1)), int(round(y2))))
    pts = [(xa, yb), (xb, yb), (xb, ya), (xa, ya)]
    return base64.b64encode(b"".join(struct.pack(">hh", x, y) for x, y in pts)).decode()


def encode_point(x, y):
    """PointToGo.PointData: i16 BE x, i16 BE y."""
    return base64.b64encode(struct.pack(">hh", int(round(x)), int(round(y)))).decode()


class Layout:
    """Map coordinates <-> PNG pixels. Map x grows to the right and y downwards: the raw
    grid turned 90° clockwise, which is how the app shows it (and the orientation
    map.render_realmap_png was checked against on an L100)."""

    def __init__(self, xs, ys, cell, margin=4):
        self.xmin = min(xs) - margin
        self.ymin = min(ys) - margin
        self.cell = cell
        self.width = (max(xs) - min(xs) + 1 + 2 * margin) * cell
        self.height = (max(ys) - min(ys) + 1 + 2 * margin) * cell

    def px(self, x, y):
        """Top-left pixel of map cell (x, y)."""
        return (x - self.xmin) * self.cell, (y - self.ymin) * self.cell

    def center(self, x, y):
        px, py = self.px(x, y)
        return px + self.cell / 2, py + self.cell / 2

    def calibration(self):
        """Three map<->pixel pairs (xiaomi-vacuum-map-card `calibration_points`)."""
        pts = [(0, 0), (100, 0), (0, 100)]
        return [{"vacuum": {"x": x, "y": y},
                 "map": {"x": round(self.center(x, y)[0], 1), "y": round(self.center(x, y)[1], 1)}}
                for x, y in pts]


def layout_for(saved, live, robot=None, charger=None, cell=None):
    """Framing of the rendered map: the saved map's bounds (stable), else the live map."""
    pts = list(saved or {}) or list(live or {})
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if not saved:
        for p in (robot, charger):
            if p:
                xs.append(p[0])
                ys.append(p[1])
    span = max(max(xs) - min(xs), max(ys) - min(ys)) + 1
    return Layout(xs, ys, cell or max(2, min(8, 1200 // span)))


def render_png(saved, live, path, robot, charger, cell=None):
    """Saved map (room colours) + live cleaned area + path + robot / charger -> (png, layout).
    `saved`/`live` = {(x, y): value}. Returns (None, None) when there is nothing to draw."""
    from PIL import Image, ImageDraw

    lay = layout_for(saved, live, robot, charger, cell)
    if lay is None:
        return None, None
    cell = lay.cell
    img = Image.new("RGBA", (lay.width, lay.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def box(x, y):
        px, py = lay.px(x, y)
        return [px, py, px + cell - 1, py + cell - 1]

    for (x, y), v in (saved or {}).items():
        if v == WALL:
            col = C_WALL
        elif v >= ROOM_BASE:
            col = ROOM_COLORS[(v - ROOM_BASE) % len(ROOM_COLORS)] + (255,)
        elif v == FLOOR:
            col = C_FLOOR
        else:
            continue
        draw.rectangle(box(x, y), fill=col)

    if live:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for (x, y), v in live.items():
            if v == FLOOR:
                od.rectangle(box(x, y), fill=C_CLEANED if saved else C_FLOOR)
            elif v == WALL and not saved:
                od.rectangle(box(x, y), fill=C_WALL)
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

    if path and len(path) > 1:
        draw.line([lay.center(x, y) for x, y in path], fill=C_PATH, width=max(1, cell // 2))
    if charger:
        cx, cy = lay.center(*charger)
        r = max(cell * 2.2, 6)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=C_CHARGER, outline=(255, 255, 255, 255))
    if robot:
        cx, cy = lay.center(*robot)
        r = max(cell * 2.6, 8)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 240))
        r2 = r * 0.72
        draw.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=C_ROBOT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), lay
