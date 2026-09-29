"""Schedule slot encoding for ILIFE Clean laser models.

The values are what an A30 Pro reported after schedules were set in its app:
Tuesday 14:30 whole home, enabled; Wednesday 16:28, living room + hall + bedroom
(rooms 0, 2, 3), 2 cycles, enabled; a disabled Friday 14:00; and an empty slot.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import pathlib
import sys

import pytest

_PKG = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)
_spec = importlib.util.spec_from_file_location("_ilife_schedule", _PKG / "tuya_schedule.py")
tuya_schedule = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tuya_schedule
_spec.loader.exec_module(tuya_schedule)

TUESDAY = "Dh4GAAACAQAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAI="
WEDNESDAY = "EBwGAAAEAQIAAAAAAAAAAAAAAAAAAAAAAgAAAA0AAAE="
FRIDAY_OFF = "DgAGAAAQAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAI="
EMPTY = "AAAAAAAAAAAAAAAAAAAAAQ=="


class TestParse:
    def test_whole_home(self):
        assert tuya_schedule.parse_schedule(TUESDAY) == {
            "time": "14:30", "days": [1], "enabled": True, "type": "global",
            "rooms": [], "cycles": 1, "program": 0,
        }

    def test_rooms(self):
        schedule = tuya_schedule.parse_schedule(WEDNESDAY)
        assert (schedule["time"], schedule["days"], schedule["type"]) == ("16:28", [2], "rooms")
        assert (schedule["rooms"], schedule["cycles"]) == ([0, 2, 3], 2)

    def test_disabled(self):
        schedule = tuya_schedule.parse_schedule(FRIDAY_OFF)
        assert (schedule["days"], schedule["enabled"]) == ([4], False)

    def test_empty_and_garbage(self):
        assert tuya_schedule.parse_schedule(EMPTY) is None
        assert tuya_schedule.parse_schedule("") is None
        assert tuya_schedule.parse_schedule("!!") is None
        assert tuya_schedule.EMPTY_SLOT == EMPTY

    def test_slots(self):
        slots = tuya_schedule.parse_schedules(
            {"Schedule1": EMPTY, "Schedule2": TUESDAY, "Schedule3": WEDNESDAY})
        assert sorted(slots) == [2, 3]


class TestBuild:
    def test_editing_keeps_every_other_byte(self):
        edited = tuya_schedule.build_schedule(TUESDAY, time="14:00", enabled=False)
        before, after = base64.b64decode(TUESDAY), base64.b64decode(edited)
        assert after[:2] == bytes([14, 0]) and after[6] == 0
        assert after[2:6] == before[2:6] and after[7:] == before[7:]

    def test_the_app_edit_reproduced(self):
        # Wednesday: rooms 0+1, 3 cycles -> rooms 0+2+3, 2 cycles, as the app did.
        before = "EBwGAAAEAQIAAAAAAAAAAAAAAAAAAAAAAwAAAAMAAAE="
        assert tuya_schedule.build_schedule(before, rooms=[0, 2, 3], cycles=2) == WEDNESDAY

    def test_rooms_switch_the_type(self):
        whole = tuya_schedule.parse_schedule(tuya_schedule.build_schedule(WEDNESDAY, rooms=[]))
        assert (whole["type"], whole["rooms"]) == ("global", [])
        rooms = tuya_schedule.parse_schedule(tuya_schedule.build_schedule(TUESDAY, rooms=[4]))
        assert (rooms["type"], rooms["rooms"]) == ("rooms", [4])

    def test_new_schedule(self):
        value = tuya_schedule.build_schedule(None, time="09:05", days=[0, 6])
        schedule = tuya_schedule.parse_schedule(value)
        assert (schedule["time"], schedule["days"], schedule["enabled"]) == ("09:05", [0, 6], True)
        assert schedule["type"] == "global" and base64.b64decode(value)[2] == 0x06

    @pytest.mark.parametrize("kwargs", [
        {"time": "25:00"}, {"days": []}, {"days": [7]}, {"rooms": [32]}, {"cycles": 4},
    ])
    def test_invalid(self, kwargs):
        with pytest.raises(ValueError):
            tuya_schedule.build_schedule(TUESDAY, **kwargs)
