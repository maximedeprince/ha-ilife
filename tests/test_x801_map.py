"""X801 map (mapType 2: A9s Pro, ZACO A9s Pro): what map.render_cells_png draws.

Cells shaped like a real ZACO A9s Pro clean, after X801Protocol.render mapped them to
the types map.py draws: the robot's track, one cell wide on every other row (2), the
outline of what was cleaned (1) and a type of its own along it (5).
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import sys

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_spec = importlib.util.spec_from_file_location("_ilife_map", _PKG / "map.py")
ilife_map = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ilife_map
_spec.loader.exec_module(ilife_map)

FLOOR = ilife_map.C_FLOOR
OBSTACLE = (0x75, 0x76, 0x7A, 255)
OTHER = (0xFB, 0xA8, 0x3C, 255)


def _clean():
    """A 12 x 9 room: outline, a track on every other row, two cells of type 5."""
    cells = [(x, y, 1) for x in range(12) for y in (0, 8)]
    cells += [(x, y, 1) for x in (0, 11) for y in range(1, 8)]
    cells += [(x, y, 2) for x in range(2, 10) for y in (2, 4, 6)]
    cells += [(1, 4, 5), (10, 4, 5)]
    return cells


def _image(**kwargs):
    from PIL import Image

    png = ilife_map.render_cells_png(_clean(), **kwargs)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _pixel(img, x, y):
    """Colour at the centre of cell (x, y): 2 cells of margin, y pointing up."""
    cell = img.width // (12 + 4)
    return img.getpixel((round((x + 2.5) * cell), round((8 - y + 2.5) * cell)))


def test_track_is_widened_into_the_cleaned_area():
    img = _image(wall=OBSTACLE, other=OTHER, floor_pad=1)
    assert _pixel(img, 5, 4) == FLOOR
    assert _pixel(img, 5, 3) == FLOOR  # the row between two passes of the robot
    assert _pixel(img, 5, 5) == FLOOR
    # without the padding the rows between two passes stay empty
    assert _pixel(_image(wall=OBSTACLE, other=OTHER), 5, 3)[3] == 0


def test_obstacles_and_other_types_stay_on_top_of_the_widened_floor():
    img = _image(wall=OBSTACLE, other=OTHER, floor_pad=1)
    assert _pixel(img, 0, 4) == OBSTACLE
    assert _pixel(img, 5, 8) == OBSTACLE
    assert _pixel(img, 1, 4) == OTHER and _pixel(img, 10, 4) == OTHER


def test_other_types_are_not_drawn_unless_asked():
    assert _pixel(_image(), 1, 4)[3] == 0
    assert _pixel(_image(), 0, 4) == ilife_map.C_WALL  # default wall colour


def test_realtime_models_draw_as_before():
    """render_png (gyro models) goes through render_cells_png with its defaults."""
    cells = {(x, y): t for x, y, t in _clean() if t in (1, 2)}
    state = {"RealTimeMap": {"MapData": ilife_map.encode_cells(cells),
                             "CurrentPiont": (5 << 16) | 4},
             "ChargerPiont": (2 << 16) | 2}
    png = ilife_map.render_png(state)
    assert png == ilife_map.render_cells_png(
        ilife_map.decode_cells(state["RealTimeMap"]["MapData"]), (5, 4), (2, 2))
    assert ilife_map.render_cells_png([]) is None


def test_rendered_image_is_bounded_and_stray_points_are_refused():
    from PIL import Image

    # A large home (~300 cells wide): cells shrink to stay within MAX_SIDE
    wide = [(0, 0, 2), (300, 0, 2), (150, 50, 1)]
    png = ilife_map.render_cells_png(wide)
    img = Image.open(io.BytesIO(png))
    assert max(img.size) <= ilife_map.MAX_SIDE

    # A stray record thousands of cells away cannot allocate a giant canvas
    assert ilife_map.render_cells_png([(0, 0, 2), (5000, 0, 2)]) is None
    assert ilife_map.cells_calibration([(0, 0), (5000, 0)]) is None


def test_calibration_matches_rendered_cell_centres():
    cells = _clean()
    cal = ilife_map.cells_calibration(cells)
    assert len(cal) == 3
    img = _image(wall=OBSTACLE, other=OTHER, floor_pad=1)
    cell = img.width // (12 + 4)
    # (0, 8) is the top-left wall cell: vacuum (0, 8) -> map pixel at its centre
    assert cal[0] == {"vacuum": {"x": 0, "y": 8}, "map": {"x": round(2.5 * cell), "y": round(2.5 * cell)}}
    assert img.getpixel((cal[0]["map"]["x"], cal[0]["map"]["y"])) == OBSTACLE
