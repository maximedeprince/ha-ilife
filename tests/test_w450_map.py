"""W450 floor-washer map (mapType 4): decoding as the ILIFEHOME app does.

Synthetic posts shaped like a real W450 clean (cell types 10 = washed floor and
2 = obstacle; RoadData path), see map_w450.py for the app references.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import struct
import sys

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_spec = importlib.util.spec_from_file_location("_ilife_map_w450", _PKG / "map_w450.py")
w450 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = w450
_spec.loader.exec_module(w450)


def _cells(*recs):
    return base64.b64encode(b"".join(struct.pack(">hhB", a, b, t) for a, b, t in recs)).decode()


def _road(*pts):
    return base64.b64encode(b"".join(struct.pack(">hh", a, b) for a, b in pts)).decode()


def _item(ts, **data):
    return {"timestamp": ts, "data": json.dumps(data)}


def test_cells_use_the_app_orientation_and_types():
    m = w450.W450Map()
    m.add_map_post(_item(1, MapData=_cells((3, 7, 10), (4, 7, 2), (5, 7, 0))))
    # (a, b) is drawn at (-b, -a); type 0 is not a cell
    assert m.cells == {(-7, -3): 10, (-7, -4): 2}


def test_later_posts_override_and_the_reset_marker_clears():
    m = w450.W450Map()
    m.add_map_post(_item(1, MapData=_cells((1, 1, 10), (2, 2, 10))))
    m.add_map_post(_item(2, MapData=_cells((1, 1, 2))))
    assert m.cells[(-1, -1)] == 2
    m.add_map_post(_item(3, MapData=_cells((32767, 32767, 0), (9, 9, 10))))
    assert m.cells == {(-9, -9): 10}


def test_road_posts_become_paths_and_the_robot_sits_at_the_end():
    m = w450.W450Map()
    m.add_road_post(_item(1, RoadData=_road((0, 0), (0, 5), (32766, 0), (2, 2), (3, 2))))
    m.add_road_post({"RoadData": _road((4, 4), (5, 4))})  # a bare property value works too
    assert m.paths == [[(0, 0), (-5, 0)], [(-2, -2), (-2, -3)], [(-4, -4), (-4, -5)]]
    assert m.robot == (-4, -5)


def test_charger_point_honours_display_switch():
    packed = (2 << 16) | (-6 & 0xFFFF)
    assert w450.charger_point({"Piont": packed, "DisplaySwitch": 1}) == (6, -2)
    assert w450.charger_point({"Piont": packed, "DisplaySwitch": 0}) is None
    assert w450.charger_point(json.dumps({"Piont": packed})) == (6, -2)


def test_render_draws_a_png():
    m = w450.W450Map()
    recs = [(a, b, 10) for a in range(-5, 6) for b in range(-8, 9)] + [(6, b, 2) for b in range(-8, 9)]
    m.add_map_post(_item(1, MapData=_cells(*recs)))
    m.add_road_post(_item(2, RoadData=_road((0, 0), (0, 5), (3, 5))))
    png = w450.render_png(m, charger=(0, 0))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png))
    # 17 columns (b) x 12 rows (a: -5..6) + margins: wider than tall, like the app
    assert img.width > img.height


def test_empty_map_renders_nothing():
    assert w450.W450Map().empty()
    assert w450.render_png(w450.W450Map()) is None


# ---- cleaning history (CleanHistory packs, app historyMapType 5) ----------------

def _hist_cells(a0, b0, height, runs, split=1):
    """Type-1 packs as the robot sends them: header + [0, type, count] runs."""
    body = b"".join(bytes([0, t, n]) for t, n in runs)
    hdr = struct.pack(">Bhhh", 1, a0, b0, height)
    step = -(-len(body) // split // 3) * 3
    return [base64.b64encode(hdr + body[i:i + step]).decode() for i in range(0, len(body), step)]


def test_history_cells_follow_the_app_decoder():
    # height 3: column 0 = rows 0..2, then column 1 ...; (x, y) = (col - B, -row - A)
    packs = dict(enumerate(_hist_cells(10, 4, 3, [(0, 1), (10, 2), (2, 3)]), 1))
    m, charger = w450.decode_history(packs)
    assert m.cells == {(-4, -11): 10, (-4, -12): 10, (-3, -10): 2, (-3, -11): 2, (-3, -12): 2}
    assert charger is None


def test_history_runs_continue_across_packs_and_path_dock_are_read():
    runs = [(10, 200), (0, 50), (2, 30)]
    one = dict(enumerate(_hist_cells(0, 0, 20, runs), 1))
    many = dict(enumerate(_hist_cells(0, 0, 20, runs, split=3), 1))
    assert len(many) == 3
    assert w450.decode_history(one)[0].cells == w450.decode_history(many)[0].cells
    n = len(many)
    many[n + 1] = base64.b64encode(b"\x02" + struct.pack(">hhhh", 1, 2, 3, 4)).decode()
    many[n + 2] = base64.b64encode(struct.pack(">Bhh", 5, 2, -6)).decode()
    m, charger = w450.decode_history(many)
    assert m.paths == [[(-2, -1), (-4, -3)]]
    assert charger == (6, -2)
    assert len(m.cells) == 230


def test_history_reencoded_for_the_card_and_gallery():
    sys.path.insert(0, str(_PKG.parent.parent))
    spec = importlib.util.spec_from_file_location("_ilife_map", _PKG / "map.py")
    gyro = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gyro
    spec.loader.exec_module(gyro)
    m, _ = w450.decode_history(dict(enumerate(_hist_cells(0, 0, 5, [(10, 12), (2, 3), (0, 5), (11, 4)]), 1)))
    b64 = w450.to_clean_map(m)
    cells = gyro.decode_clean_bitmap(b64)
    got = {(x, y): t for x, y, t in cells}
    minx = min(c[0] for c in m.cells); miny = min(c[1] for c in m.cells)
    want = {(x - minx, y - miny): (2 if t in w450.FLOOR_COLORS else 1) for (x, y), t in m.cells.items()}
    assert got == want
    assert gyro.render_clean_map_png(b64)[:4] == b"\x89PNG"


def test_calibration_points_map_cells_to_pixels():
    m = w450.W450Map()
    m.add_map_post({"MapData": _cells(*[(a, b, 10) for a in range(10) for b in range(20)])})
    cal = w450.calibration(m)
    assert len(cal) == 3
    (p0, p1, p2) = cal
    assert p1["vacuum"]["x"] - p0["vacuum"]["x"] == 10 and p1["map"]["x"] > p0["map"]["x"]
    assert p2["vacuum"]["y"] - p0["vacuum"]["y"] == 10 and p2["map"]["y"] > p0["map"]["y"]


# ---- a new clean when RealTimeMapStart is not re-reported --------------------------

def test_cleantime_going_back_starts_a_new_map():
    m = w450.W450Map()
    m.add_map_post(_item(1, MapData=_cells((1, 1, 10)), CleanTime=3000))
    m.add_road_post(_item(1, RoadData=_road((0, 0), (0, 5)), CleanTime=3000))
    m.add_map_post(_item(2, MapData=_cells((2, 2, 10)), CleanTime=3004))  # same clean
    assert len(m.cells) == 2
    m.add_map_post(_item(3, MapData=_cells((9, 9, 10)), CleanTime=72))     # next day
    assert m.cells == {(-9, -9): 10} and m.paths == []
    m.add_road_post(_item(3, RoadData=_road((9, 9), (9, 8)), CleanTime=72))
    assert len(m.paths) == 1  # same CleanTime: not another reset


def test_current_clean_keeps_only_the_latest():
    items = [_item(t, CleanTime=ct) for t, ct in
             [(1, 0), (2, 4), (3, 4000), (4, 72), (5, 76), (6, 80)]]
    assert [i["timestamp"] for i in w450.current_clean(items)] == [4, 5, 6]
    assert w450.current_clean(items[:3]) == items[:3]
    assert w450.current_clean([]) == []
