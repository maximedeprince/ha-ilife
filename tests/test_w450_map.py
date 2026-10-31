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
