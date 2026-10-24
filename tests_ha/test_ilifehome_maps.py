"""ILIFEHOME map protocols, with Home Assistant (#35).

The integration is set up for real with the Alibaba cloud replaced: a V3x (gyro, real-time
map) and a SLAM model (saved map with rooms). What is checked here only shows at this
level: which features each vacuum offers, what the room clean writes, and that a map the
decoder cannot read costs the map and nothing else.

The saved map is synthetic, built like the one in tests/test_slam_map.py.
"""
from __future__ import annotations

import base64
from datetime import timedelta
import struct
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ilife.const import DOMAIN
from custom_components.ilife.diagnostics import async_get_config_entry_diagnostics

IOT_ID = "MqdfUQpJAMnFesegxTGV000000"
V3X = "a1uDIqJ4a8u"
L100 = "a1xvBzVBKC7"
MAP_ID = 1700000000


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _saved_map() -> bytes:
    """Walls around two rooms: value 11 (room bit 1) left, 12 (room bit 2) right."""
    width, rows = 20, 10
    flat = []
    for r in range(rows):
        for c in range(width):
            if r in (0, rows - 1) or c in (0, width - 1):
                flat.append(1)
            else:
                flat.append(11 if c < width // 2 else 12)
    out = bytearray([1, 50]) + struct.pack(">hhh", 5, 10, width)
    i = 0
    while i < len(flat):
        n = 1
        while i + n < len(flat) and flat[i + n] == flat[i] and n < 255:
            n += 1
        out += bytes([flat[i], n])
        i += n
    return bytes(out)


SLAM_STATE = {
    "WorkMode": 16,
    "BatteryState": 100,
    "SaveMap": {"SelectedMapId": MAP_ID, "SaveMapId": ""},
    "SaveMapDataX9_1": {"MapId": MAP_ID, "TimeStamp": MAP_ID, "PackId": 65537,
                        "MapData1": _b64(_saved_map())},
    "MapRoomInfo1": _b64(f"{MAP_ID},Home,1,Living room,2,Kitchen".encode()),
    "RealMapRoadData": {"RoadData": "", "CurrentPoint": 0},
    "WiFiInfo": {"RSSI": 97, "IP": -1062731638, "MAC": "NCADkZL4"},
}
V3X_STATE = {
    "WorkMode": 9,
    "BatteryState": 99,
    "RealTimeMap": {"MapData": _b64(struct.pack(">hhB", 1, 2, 2) * 3), "CurrentPiont": 0},
}


class FakeAccount:
    """Stands in for ILifeAccount: the cloud calls ILifeDevice makes, and the writes."""

    instance: FakeAccount
    product_key = L100
    state: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        self.state = dict(type(self).state)
        self.writes: list[dict] = []
        FakeAccount.instance = self

    def login(self):
        return [{"iotId": IOT_ID, "productKey": self.product_key, "nickName": "Robot",
                 "productName": "ILIFE"}]

    def device_status(self):
        return {IOT_ID: True}

    def _iot(self, path, d, api_ver="1.0.2"):
        if path == "/thing/properties/get":
            return {"code": 200, "data": {k: {"value": v} for k, v in self.state.items()}}
        if path == "/thing/properties/set":
            self.writes.append(d["items"])
            return {"code": 200}
        return {"code": 200, "data": {"items": []}}


async def _setup(hass: HomeAssistant, product_key: str, state: dict) -> FakeAccount:
    FakeAccount.product_key, FakeAccount.state = product_key, state
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"backend": "ilifehome", "email": "user@example.com", "password": "pw",
              "region": "eu", "brand": "ilife"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return FakeAccount.instance


@pytest.fixture(autouse=True)
def fake_cloud():
    with (
        patch("custom_components.ilife.ILifeAccount", FakeAccount),
        # the card's resource: Lovelace is not loaded here
        patch("custom_components.ilife._async_register_frontend"),
        patch("custom_components.ilife.ILifeCoordinator._archive_maps"),
    ):
        yield


async def test_a_v3x_keeps_its_real_time_map_and_nothing_else(hass: HomeAssistant) -> None:
    await _setup(hass, V3X, V3X_STATE)
    vacuum = hass.states.get("vacuum.robot")
    assert vacuum.state == "docked"
    features = VacuumEntityFeature(vacuum.attributes["supported_features"])
    assert VacuumEntityFeature.SEND_COMMAND not in features
    assert VacuumEntityFeature.CLEAN_AREA not in features
    assert "rooms" not in vacuum.attributes
    assert hass.states.get("camera.robot_map").state != "unavailable"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "clean_rooms",
                                       {"entity_id": "vacuum.robot", "rooms": [1]},
                                       blocking=True)


async def test_a_slam_model_cleans_rooms_by_name(hass: HomeAssistant) -> None:
    account = await _setup(hass, L100, {**SLAM_STATE, "WorkMode": 6})
    vacuum = hass.states.get("vacuum.robot")
    assert vacuum.attributes["rooms"] == {1: "Living room", 2: "Kitchen"}
    features = VacuumEntityFeature(vacuum.attributes["supported_features"])
    assert VacuumEntityFeature.SEND_COMMAND in features
    assert VacuumEntityFeature.CLEAN_AREA in features
    assert "calibration_points" in hass.states.get("camera.robot_map").attributes

    with patch("custom_components.ilife.vacuum.asyncio.sleep"):
        await hass.services.async_call(DOMAIN, "clean_rooms",
                                       {"entity_id": "vacuum.robot", "rooms": ["kitchen"]},
                                       blocking=True)
    # busy: standby first, as the app does, then the room bit
    assert account.writes[0] == {"WorkMode": 2}
    assert account.writes[1] == {
        "CleanPartitionData": {"PartitionData": 2, "CleanLoop": 1, "Enable": 1}}


async def test_zone_coordinates_out_of_range_are_refused(hass: HomeAssistant) -> None:
    account = await _setup(hass, L100, SLAM_STATE)
    with pytest.raises(HomeAssistantError, match="between"):
        await hass.services.async_call(
            "vacuum", "send_command",
            {"entity_id": "vacuum.robot", "command": "app_zoned_clean",
             "params": [[0, 0, 40000, 10]]}, blocking=True)
    assert account.writes == []


async def test_without_a_saved_map_the_last_live_map_is_shown(hass: HomeAssistant) -> None:
    live = {"UpdateTime": MAP_ID + 10, "PackId": 65537,
            "MapData1": _b64(bytes([0]) + _saved_map()[1:])}
    state = {k: v for k, v in SLAM_STATE.items() if not k.startswith("SaveMap")}
    await _setup(hass, L100, {**state, "RealMapData_1": live, "RealTimeMapStart": MAP_ID})
    assert hass.states.get("camera.robot_map").state != "unavailable"


async def test_an_unreadable_map_costs_the_map_only(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    with patch("custom_components.ilife.protocols.SlamX9Protocol.async_update",
               side_effect=ValueError("a map from the future")):
        await _setup(hass, L100, SLAM_STATE)
        for _ in range(3):
            freezer.tick(timedelta(seconds=30))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert hass.states.get("vacuum.robot").state == "docked"
    assert hass.states.get("sensor.robot_battery").state == "100"
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "could not be read" in r.getMessage()]
    assert len(warnings) == 1  # once, not every poll


async def test_diagnostics_describe_the_map_without_drawing_it(hass: HomeAssistant) -> None:
    await _setup(hass, L100, SLAM_STATE)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    device = diag["devices"][0]
    text = str(device)
    assert _b64(_saved_map()) not in text
    assert "Kitchen" not in text and "Living room" not in text
    assert "NCADkZL4" not in text and "-1062731638" not in text
    assert device["state"]["SaveMapDataX9_1"]["MapData1"].endswith("bytes>")
    assert device["state"]["SaveMapDataX9_1"]["MapData1_header"] == _saved_map()[:8].hex()
    assert device["map_protocol"]["key"] == "slam_x9"
    assert device["map_protocol"]["room_ids"] == [1, 2]
    assert device["map_protocol"]["named_rooms"] == 2
