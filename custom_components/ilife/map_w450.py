"""W450 / W400 / W455 floor-washer map (app mapType 4).

Mirrors the ILIFEHOME app (MapW450Presenter, HistoryMapW450Repository,
MapView.drawMapW450):

* The map is the union of every RealTimeMap post since RealTimeMapStart (property
  timeline); each post's MapData is a run of 5-byte records [int16 a][int16 b][uint8 type],
  a later record overrides the type of the same cell.
* The app plots a record at (x=-b, y=-a) — that is what makes it match the app's view.
* (32767, 32767) is not a cell but a "map cleared" marker. Type 0 is skipped (history).
* Colours per type (res/values/colors.xml wpt_N): 0, 1, 10..15 are floor shades; every
  other type is an obstacle (#75767A).
* The path is RealMapRoadData.RoadData: 4-byte [int16 a][int16 b] points, same
  transform; each post starts a new sub-path, a 32766 coordinate breaks the line. The
  robot is drawn at the end of the path, the dock at ChargerPiont (when displayed).
"""
from __future__ import annotations

import base64
import io
import json
import struct
from typing import Any

FLOOR_COLORS = {
    0: (0x4B, 0x4F, 0x55), 1: (0x62, 0x82, 0xCE), 10: (0x62, 0x82, 0xCE),
    11: (0x3A, 0x69, 0xD5), 12: (0x1E, 0x56, 0xD9), 13: (0xFB, 0xA8, 0x3C),
    14: (0xF5, 0x8A, 0x00), 15: (0xF5, 0x78, 0x19),
}
OBSTACLE = (0x75, 0x76, 0x7A)
PATH = (255, 255, 255, 215)
ROBOT = (231, 76, 60, 255)
DOCK = (46, 204, 113, 255)
RESET = 32767
BREAK = 32766


def _post(item: Any) -> dict:
    """A timeline item / property value -> its dict ({"MapData": ...} etc.)."""
    if isinstance(item, dict) and "data" in item:
        item = item["data"]
    if isinstance(item, str):
        try:
            item = json.loads(item)
        except ValueError:
            return {}
    return item if isinstance(item, dict) else {}


def _b64(s: Any) -> bytes:
    if not s or not isinstance(s, str):
        return b""
    try:
        return base64.b64decode(s)
    except (ValueError, TypeError):
        return b""


def _s16(v: int) -> int:
    return struct.unpack(">h", struct.pack(">H", v & 0xFFFF))[0]


def unpack_point(v: Any) -> tuple[int, int] | None:
    """Packed (a<<16)|(b & 0xFFFF) -> app screen cell (-b, -a)."""
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    return (-_s16(v), -_s16(v >> 16))


class W450Map:
    """Accumulates RealTimeMap / RealMapRoadData posts into the current clean's map."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], int] = {}
        self.paths: list[list[tuple[int, int]]] = []
        self._clean_time: int | None = None

    def reset(self) -> None:
        self.cells.clear()
        self.paths.clear()
        self._clean_time = None

    def _new_clean(self, post: dict) -> None:
        """Posts carry CleanTime (s since the clean started; it keeps counting through
        a pause). When it goes back, a new clean has started — RealTimeMapStart is not
        always re-reported then — so the previous clean's map must go."""
        ct = post.get("CleanTime")
        if not isinstance(ct, int) or isinstance(ct, bool):
            return
        if self._clean_time is not None and ct < self._clean_time:
            self.cells.clear()
            self.paths.clear()
        self._clean_time = ct

    def add_map_post(self, post: Any) -> None:
        post = _post(post)
        self._new_clean(post)
        raw = _b64(post.get("MapData"))
        for i in range(0, len(raw) - len(raw) % 5, 5):
            a, b = struct.unpack(">hh", raw[i:i + 4])
            t = raw[i + 4]
            if a == RESET and b == RESET:
                self.cells.clear()
                continue
            if t == 0:
                continue
            self.cells[(-b, -a)] = t

    def add_road_post(self, post: Any) -> None:
        post = _post(post)
        self._new_clean(post)
        raw = _b64(post.get("RoadData"))
        cur: list[tuple[int, int]] = []
        for i in range(0, len(raw) - len(raw) % 4, 4):
            a, b = struct.unpack(">hh", raw[i:i + 4])
            if BREAK in (a, b):
                if len(cur) > 1:
                    self.paths.append(cur)
                cur = []
                continue
            cur.append((-b, -a))
        if len(cur) > 1:
            self.paths.append(cur)
        elif len(cur) == 1:
            self.paths.append(cur)  # a lone point still moves the robot marker

    @property
    def robot(self) -> tuple[int, int] | None:
        for path in reversed(self.paths):
            if path:
                return path[-1]
        return None

    def empty(self) -> bool:
        return not self.cells and not any(len(p) > 1 for p in self.paths)


def clean_time(item: Any) -> int | None:
    ct = _post(item).get("CleanTime")
    return ct if isinstance(ct, int) and not isinstance(ct, bool) else None


def current_clean(items: list) -> list:
    """Oldest-first timeline items -> only those of the latest clean (after the last
    point where CleanTime went back)."""
    start = 0
    prev = None
    for i, it in enumerate(items):
        ct = clean_time(it)
        if ct is None:
            continue
        if prev is not None and ct < prev:
            start = i
        prev = ct
    return items[start:]


def charger_point(value: Any) -> tuple[int, int] | None:
    """ChargerPiont {"Piont": packed, "DisplaySwitch": 0/1} -> cell, if displayed."""
    v = _post(value)
    if not v or v.get("DisplaySwitch") == 0:
        return None
    return unpack_point(v.get("Piont"))


def geometry(m: W450Map, charger: tuple[int, int] | None = None,
             max_px: int = 720) -> tuple[int, int, int, int, int] | None:
    """(minx, miny, cols, rows, cell px) of the rendered image, or None when empty."""
    pts = list(m.cells) + [p for path in m.paths for p in path]
    if charger:
        pts.append(charger)
    if not pts:
        return None
    margin = 3
    minx = min(p[0] for p in pts) - margin
    miny = min(p[1] for p in pts) - margin
    cols = max(p[0] for p in pts) - minx + 1 + margin
    rows = max(p[1] for p in pts) - miny + 1 + margin
    cell = max(4, min(40, max_px // max(cols, rows)))
    return minx, miny, cols, rows, cell


def calibration(m: W450Map, charger: tuple[int, int] | None = None) -> list[dict] | None:
    """xiaomi-vacuum-map-card style calibration_points: map cell -> image pixel."""
    g = geometry(m, charger)
    if g is None:
        return None
    minx, miny, _cols, _rows, cell = g

    def px(x: int, y: int) -> dict:
        return {"x": round((x - minx) * cell + cell / 2), "y": round((y - miny) * cell + cell / 2)}

    x0, y0 = minx + 3, miny + 3
    return [{"vacuum": {"x": x, "y": y}, "map": px(x, y)}
            for x, y in ((x0, y0), (x0 + 10, y0), (x0, y0 + 10))]


def render_png(m: W450Map, charger: tuple[int, int] | None = None,
               max_px: int = 720) -> bytes | None:
    """PNG of the map, oriented like the app."""
    from PIL import Image, ImageDraw

    g = geometry(m, charger, max_px)
    if g is None:
        return None
    minx, miny, cols, rows, cell = g
    ss = 3  # supersampling for smooth path / markers
    cs = cell * ss
    img = Image.new("RGBA", (cols * cs, rows * cs), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def box(x: int, y: int) -> list[int]:
        c, r = (x - minx) * cs, (y - miny) * cs
        return [c, r, c + cs - 1, r + cs - 1]

    def center(p: tuple[int, int]) -> tuple[float, float]:
        return ((p[0] - minx) * cs + cs / 2, (p[1] - miny) * cs + cs / 2)

    # floor first, obstacles on top (MapView: slam paths, then obstaclePath)
    for (x, y), t in m.cells.items():
        if t in FLOOR_COLORS:
            draw.rectangle(box(x, y), fill=FLOOR_COLORS[t] + (255,))
    for (x, y), t in m.cells.items():
        if t not in FLOOR_COLORS:
            draw.rectangle(box(x, y), fill=OBSTACLE + (255,))
    width = max(2, cs // 5)
    for path in m.paths:
        if len(path) > 1:
            draw.line([center(p) for p in path], fill=PATH, width=width, joint="curve")
    for pos, color, scale in ((charger, DOCK, 0.9), (m.robot, ROBOT, 1.1)):
        if pos:
            cx, cy = center(pos)
            r = cs * scale
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color,
                         outline=(255, 255, 255, 255), width=max(2, cs // 6))
    img = img.resize((cols * cell, rows * cell), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
#  Cleaning history (CleanHistory.CleanMapData packs, app historyMapType 5)
# --------------------------------------------------------------------------- #
def decode_history(packs: dict[Any, str]) -> tuple[W450Map, tuple[int, int] | None]:
    """A finished clean's map from its CleanHistory packs (PackId -> CleanMapData),
    as HistoryDetailActivity_x1000.drawHistoryMapW450 decodes it. Each pack starts with
    a type byte:
      1: [int16 A][int16 B][int16 H] + run-length cells — the runs of every type-1 pack
         form one stream of 3-byte records [?, type, count] (the app ignores the first
         byte); cells fill columns of height H: (x, y) = (column - B, -row - A)
      2: path points, 4-byte [int16 a][int16 b] -> (-b, -a)
      5: dock position [int16 hi][int16 lo] -> (-lo, -hi)
      7: cleaning-zone rectangle (not drawn here)
    """
    m = W450Map()
    charger = None
    stream = bytearray()
    a0 = b0 = height = 0
    path: list[tuple[int, int]] = []
    for pid in sorted(packs, key=lambda k: (k is None, k if isinstance(k, int) else 0)):
        raw = _b64(packs[pid])
        if not raw:
            continue
        kind = raw[0]
        if kind == 1 and len(raw) >= 7:
            a0, b0, height = struct.unpack(">hhh", raw[1:7])
            stream += raw[7:]
        elif kind == 2:
            for i in range(1, len(raw) - 3, 4):
                a, b = struct.unpack(">hh", raw[i:i + 4])
                path.append((-b, -a))
        elif kind == 5 and len(raw) >= 5:
            hi, lo = struct.unpack(">hh", raw[1:5])
            charger = (-lo, -hi)
    if height > 0:
        row = col = 0
        for k in range(2, len(stream), 3):
            typ, cnt = stream[k - 1], stream[k]
            for _ in range(cnt):
                if typ:
                    m.cells[(col - b0, -row - a0)] = typ
                if row < height - 1:
                    row += 1
                else:
                    col += 1
                    row = 0
    if path:
        m.paths.append(path)
    return m, charger


def to_clean_map(m: W450Map) -> str | None:
    """Re-encode cells as the 2bpp CleanMapData the card / gallery understand
    ([0x01][bytes per row] + rows, 4 cells per byte, high bits first):
    floor shades -> 2, obstacles -> 1. Oriented like the app."""
    if not m.cells:
        return None
    xs = [c[0] for c in m.cells]
    ys = [c[1] for c in m.cells]
    minx, miny = min(xs), min(ys)
    w, h = max(xs) - minx + 1, max(ys) - miny + 1
    bpr = (w + 3) // 4
    if bpr > 255:
        return None
    out = bytearray([0x01, bpr]) + bytes(bpr * h)
    for (x, y), t in m.cells.items():
        v = 2 if t in FLOOR_COLORS else 1
        cx, cy = x - minx, y - miny
        i = 2 + cy * bpr + cx // 4
        out[i] |= v << ((3 - cx % 4) * 2)
    return base64.b64encode(bytes(out)).decode()
