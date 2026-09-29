"""Room names and room-clean encoding for ILIFE Clean laser models.

The values below are the ones an A30 Pro and its app exchanged, read from the
Tuya device log: the app selecting one room twice, then two rooms twice.
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
_spec = importlib.util.spec_from_file_location("_ilife_rooms", _PKG / "tuya_rooms.py")
tuya_rooms = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tuya_rooms
_spec.loader.exec_module(tuya_rooms)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class TestRoomNames:
    def test_names_are_keyed_by_the_map_room_id(self):
        info = _b64("1762447000,mapName,4,Office,0,Living room,2,Hall,1,Bathroom,3,Łóżko")
        assert tuya_rooms.parse_room_names({"MapRoomInfo1": info}) == {
            4: "Office", 0: "Living room", 2: "Hall", 1: "Bathroom", 3: "Łóżko",
        }

    def test_a_list_continues_in_the_next_dp(self):
        properties = {
            "MapRoomInfo1": _b64("1,mapName,0,Living room,1,Ki"),
            "MapRoomInfo2": _b64("tchen"),
        }
        assert tuya_rooms.parse_room_names(properties) == {0: "Living room", 1: "Kitchen"}

    def test_no_names(self):
        assert tuya_rooms.parse_room_names({"MapRoomInfo1": ""}) == {}
        assert tuya_rooms.parse_room_names({}) == {}

    def test_garbage_is_ignored(self):
        assert tuya_rooms.parse_room_names({"MapRoomInfo1": "!!!not base64"}) == {}


class TestPrograms:
    # CleanSettings as the app wrote it when switching program (A30 Pro device log).
    STANDARD = "aQzOmGkM0KcFAQAAYgEAYgIBYgMjYQQiYgAgIQEgIQIgIQMgIQQgIQ=="
    PLAN_1 = "aQzOmGkM0KcFAgAAYgEAYgIBYgMjYQQiYgAgIQEgIQIgIQMgIQQgIQ=="
    PLAN_2 = "aQzOmGkM0KcFAwAAYgEAYgIBYgMjYQQiYgAgIQEgIQIgIQMgIQQgIQ=="

    def test_active_program(self):
        assert tuya_rooms.active_program(self.STANDARD) == "standard"
        assert tuya_rooms.active_program(self.PLAN_1) == "plan_1"
        assert tuya_rooms.active_program(self.PLAN_2) == "plan_2"

    def test_switching_changes_only_the_program_byte(self):
        assert tuya_rooms.with_program(self.STANDARD, "plan_1") == self.PLAN_1
        assert tuya_rooms.with_program(self.PLAN_1, "plan_2") == self.PLAN_2
        assert tuya_rooms.with_program(self.PLAN_2, "standard") == self.STANDARD

    def test_unknown_or_missing(self):
        assert tuya_rooms.active_program("") is None
        assert tuya_rooms.active_program(None) is None
        with pytest.raises(ValueError):
            tuya_rooms.with_program(self.STANDARD, "plan_9")
        with pytest.raises(ValueError):
            tuya_rooms.with_program("", "plan_1")


class TestCleanRoomsValue:
    def test_matches_what_the_app_sent(self):
        assert tuya_rooms.clean_rooms_value([0], passes=2) == "AQIAAAAB"
        assert tuya_rooms.clean_rooms_value([2, 3], passes=2) == "AQIAAAAM"

    def test_single_pass(self):
        assert base64.b64decode(tuya_rooms.clean_rooms_value([1, 4])) == bytes(
            [1, 1, 0, 0, 0, 0b10010]
        )

    @pytest.mark.parametrize("rooms, passes", [([], 1), ([32], 1), ([-1], 1), ([0], 0), ([0], 3)])
    def test_invalid(self, rooms, passes):
        with pytest.raises(ValueError):
            tuya_rooms.clean_rooms_value(rooms, passes)
