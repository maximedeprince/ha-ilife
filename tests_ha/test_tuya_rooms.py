"""ILIFE Clean rooms when the app has no room names (#41).

An A30 Pro whose rooms were never renamed in the app reports empty MapRoomInfo DPs, yet
its map has the rooms and the app shows them as "Room1", "Room2"... The rooms then come
from the map: the vacuum lists them, and `ilife.clean_rooms` finds them by name.

The map is synthetic (the format of tests/test_tuya_map.py): three rooms, no names.
"""
from __future__ import annotations

import base64
import json
import pathlib
import struct
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ilife.const import DOMAIN

A30_PRO = json.loads(
    (pathlib.Path(__file__).parents[1] / "tests" / "fixtures" / "ilife_a30_pro.json").read_text()
)
DEVICE = {"id": "bf0123456789abcdefgh", "category": "sd", "name": "Robot",
          "product_name": "ILIFE A30 Pro", "online": True}
IDLE = base64.b64encode(bytes(6)).decode()  # CleanPartitionData when nothing runs


def _a30_map(room_ids=(0, 1, 2), width=6, height=4) -> bytes:
    """A version 1 map: one column band per room, no names, as an A30 Pro sends it."""
    cells = bytearray()
    for _row in range(height):
        for col in range(width):
            cells.append(room_ids[col * len(room_ids) // width] << 2)
    rooms = b""
    for room_id in room_ids:
        block = bytearray(47)
        struct.pack_into(">4H", block, 0, room_id, 0, 0, 0)
        rooms += bytes(block)
    body = bytes(cells) + bytes([0, len(room_ids)]) + rooms
    literals = len(body) - 15
    block = bytes([0xF0]) + b"\xff" * (literals // 255) + bytes([literals % 255]) + body
    header = bytearray([1]) + (1).to_bytes(2, "big") + b"\x01"
    header += struct.pack(">7HIH", width, height, 30, 20, 5, 30, 20, len(body), len(block))
    return bytes(header) + block


class FakeClient:
    """Stands in for TuyaClient: an A30 Pro with rooms on its map and none named."""

    instance: FakeClient

    def __init__(self, *args, **kwargs) -> None:
        self.properties = {"MapRoomInfo1": "", "MapRoomInfo2": "", "MapRoomInfo3": "",
                           "CleanPartitionData": IDLE}
        self.issued: list[dict] = []
        FakeClient.instance = self

    def usage(self) -> dict:
        return {}

    def list_devices(self):
        return [DEVICE]

    def device_detail(self, device_id):
        return {"online": True,
                "status": [{"code": k, "value": v} for k, v in A30_PRO["status"].items()]}

    def device_specification(self, device_id):
        return A30_PRO["specification"]

    def device_model(self, device_id):
        return {}

    def device_properties(self, device_id):
        return dict(self.properties)

    def stored_map_records(self, device_id, count=5):
        return [{"id": "100", "time": 1_700_000_000, "extend": ""}]

    def latest_stored_map(self, device_id):
        return self.stored_map_records(device_id)[0]

    def stored_map_files(self, device_id, record):
        return [{"map_type": 0, "payload": _a30_map()}]

    def realtime_map_files(self, device_id):
        return []

    def send_commands(self, device_id, commands):
        return True

    def issue_properties(self, device_id, properties):
        self.issued.append(properties)


@pytest.fixture
async def client(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"backend": "ilife_clean", "access_id": "id", "access_secret": "secret",
              "uid": "uid", "region": "eu"},
        unique_id="ilife_clean_uid",
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ilife.TuyaClient", FakeClient),
        patch("custom_components.ilife._async_register_frontend"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield FakeClient.instance


def _mask(value: str) -> int:
    raw = base64.b64decode(value)
    return int.from_bytes(raw[2:6], "big")


async def test_rooms_come_from_the_map_when_the_app_names_none(
    hass: HomeAssistant, client: FakeClient
) -> None:
    rooms = hass.states.get("vacuum.robot").attributes["rooms"]
    assert rooms == {0: "Room 1", 1: "Room 2", 2: "Room 3"}

    # by name as this integration writes it, as the app writes it, or by number
    for wanted, bit in (("Room 1", 0), ("room2", 1), ("ROOM 3", 2), (1, 1)):
        await hass.services.async_call(
            DOMAIN, "clean_rooms", {"entity_id": "vacuum.robot", "rooms": [wanted]},
            blocking=True)
        assert _mask(client.issued[-1]["CleanPartitionData"]) == 1 << bit

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "clean_rooms", {"entity_id": "vacuum.robot", "rooms": ["Kitchen"]},
            blocking=True)
    assert err.value.translation_key == "room_unknown"
    assert err.value.translation_placeholders["rooms"] == "Room 1 (0), Room 2 (1), Room 3 (2)"


async def test_names_from_the_app_still_win(hass: HomeAssistant) -> None:
    named = base64.b64encode(b"1762447000,mapName,1,Kitchen").decode()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"backend": "ilife_clean", "access_id": "id", "access_secret": "secret",
              "uid": "uid", "region": "eu"},
        unique_id="ilife_clean_uid",
    )
    entry.add_to_hass(hass)

    class Named(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()
            self.properties["MapRoomInfo1"] = named

    with (
        patch("custom_components.ilife.TuyaClient", Named),
        patch("custom_components.ilife._async_register_frontend"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    rooms = hass.states.get("vacuum.robot").attributes["rooms"]
    assert rooms == {0: "Room 1", 1: "Kitchen", 2: "Room 3"}


async def test_no_room_buttons_without_room_cleaning(hass: HomeAssistant) -> None:
    """A model without CleanPartitionData (e.g. the V20) keeps no `rooms` attribute,
    even though its map has rooms: the card would show buttons that do nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"backend": "ilife_clean", "access_id": "id", "access_secret": "secret",
              "uid": "uid", "region": "eu"},
        unique_id="ilife_clean_uid",
    )
    entry.add_to_hass(hass)

    class NoRoomClean(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()
            self.properties = {}

    with (
        patch("custom_components.ilife.TuyaClient", NoRoomClean),
        patch("custom_components.ilife._async_register_frontend"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert "rooms" not in hass.states.get("vacuum.robot").attributes
    assert hass.states.get("camera.robot_map").attributes["rooms"]  # the map still has them
