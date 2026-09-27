"""Stored map records as cleaning history: what `extend` says about each clean.

Values from an A30 Pro's record list, checked against its app's history (the
21:53 clean: 2 m², 3 min) and against a clean watched live (21:29, ~10 m²,
~11 min).
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import pathlib
import sys

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_spec = importlib.util.spec_from_file_location("_ilife_tuya_api", _PKG / "tuya_api.py")
tuya_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tuya_api
_spec.loader.exec_module(tuya_api)


def test_extend_gives_start_area_and_duration():
    assert tuya_api.parse_record_extend("00008_20260927_215313_002_003_04379_00710_00003") == {
        "started": datetime.datetime(2026, 9, 27, 21, 53, 13), "area": 2, "duration": 3,
    }
    clean = tuya_api.parse_record_extend("00004_20260927_212954_010_011_04619_01919_00003")
    assert (clean["area"], clean["duration"]) == (10, 11)


def test_unreadable_extend():
    for extend in (None, "", "garbage", "00001_2026_x_1_2", "00001_20261399_000000_001_002"):
        assert tuya_api.parse_record_extend(extend) is None
