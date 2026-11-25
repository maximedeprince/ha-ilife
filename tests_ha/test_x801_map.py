"""X801 map (A9s Pro, ZACO A9s Pro) in Home Assistant: rebuilt from the property
timeline and kept once the robot is docked."""
from __future__ import annotations

import base64
import io
import json
import struct
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant

from custom_components.ilife.const import DOMAIN

IOT_ID = "PYr3lR1niXJTZrKtnSjb000000"
ZACO_A9S_PRO = "a2n0QiJDnzc"
START = 1794742456000


def _cells(*recs):
    return base64.b64encode(b"".join(struct.pack(">hhB", x, y, t) for x, y, t in recs)).decode()


class FakeAccount:
    instance: "FakeAccount"

    def __init__(self, *args, **kwargs) -> None:
        # docked after a clean: the cloud only holds the last post, three cells of it
        self.state = {"WorkMode": 9, "BatteryState": 80, "RealTimeMapStart": START // 1000,
                      "RealTimeMap": {"MapData": _cells((9, 1, 4), (9, 2, 4), (9, 3, 2)),
                                      "CurrentPiont": (1 << 16) | 1, "CleanArea": 2664,
                                      "CleanTime": 1928},
                      "ChargerPoint": {"Piont": (1 << 16) | 1, "DisplaySwitch": 1}}
        track = [(x, y, 4) for x in range(2, 10) for y in (2, 4, 6)]
        outline = ([(x, y, 2) for x in range(12) for y in (0, 8)]
                   + [(x, y, 2) for x in (0, 11) for y in range(1, 8)])
        self.timeline = {"RealTimeMap": [
            (START + 10, {"MapData": _cells(*track[:12]), "CleanTime": 10}),
            (START + 20, {"MapData": _cells(*track[12:], *outline), "CleanTime": 20}),
            (START + 30, {"MapData": _cells((1, 4, 5), (10, 4, 5)), "CleanTime": 30}),
        ]}
        self.timeline_calls: list[dict] = []
        FakeAccount.instance = self

    def login(self):
        return [{"iotId": IOT_ID, "productKey": ZACO_A9S_PRO, "nickName": "Robot",
                 "productName": "ZACO A9s Pro"}]

    def device_status(self):
        return {IOT_ID: True}

    def _iot(self, path, d, api_ver="1.0.2"):
        if path == "/thing/properties/get":
            data = {k: {"value": v, "time": START} for k, v in self.state.items()}
            return {"code": 200, "data": data}
        if path == "/thing/property/timeline/get":
            self.timeline_calls.append(d)
            sel = sorted(((ts, v) for ts, v in self.timeline.get(d["identifier"], [])
                          if d["start"] <= ts <= d["end"]),
                         key=lambda e: e[0], reverse=not d.get("ordered", True))
            items = [{"timestamp": ts, "data": json.dumps(v)}
                     for ts, v in sel[:d.get("pageSize", 200)]]
            return {"code": 200, "data": {"items": items}}
        return {"code": 200, "data": {"items": []}}


@pytest.fixture(autouse=True)
def fake_cloud():
    with (
        patch("custom_components.ilife.ILifeAccount", FakeAccount),
        patch("custom_components.ilife._async_register_frontend"),
        patch("custom_components.ilife.ILifeCoordinator._archive_maps"),
    ):
        yield


async def _setup(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u@example.com", data={
        "backend": "ilifehome", "email": "u@example.com", "password": "pw",
        "region": "eu", "brand": "zaco"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return next(iter(hass.data[DOMAIN][entry.entry_id]["coordinators"].values()))


async def test_docked_robot_shows_the_whole_last_clean(hass: HomeAssistant) -> None:
    coord = await _setup(hass)
    acc = FakeAccount.instance
    assert coord.protocol.key == "x801"
    # the whole clean, read once from RealTimeMapStart's report time — not the three
    # cells of the last post, which is all the polled state holds
    assert [c["start"] for c in acc.timeline_calls] == [START]
    diag = coord.protocol.diagnostics()
    assert diag["cells"] == 64 and diag["cell_types"] == {4: 24, 2: 38, 5: 2}
    assert diag["timeline_posts"] == 3

    cam = hass.states.get("camera.robot_map")
    assert cam.state != "unavailable"
    # docked card keeps the camera (instead of a garbled history thumbnail): calibration_points
    assert len(cam.attributes["calibration_points"]) == 3
    image = await async_get_image(hass, "camera.robot_map")
    from PIL import Image
    img = Image.open(io.BytesIO(image.content)).convert("RGBA")
    cell = img.width // (12 + 4)  # 12 cells and 2 of margin on each side

    def pixel(x, y):
        return img.getpixel((round((x + 2.5) * cell), round((8 - y + 2.5) * cell)))

    assert pixel(5, 4) == (124, 173, 255, 255)   # track (type 4): cleaned floor
    assert pixel(5, 3) == (124, 173, 255, 255)   # between two passes: cleaned too
    assert pixel(0, 4) == (0x75, 0x76, 0x7A, 255)  # outline (type 2): obstacle
    assert pixel(10, 4) == (0xFB, 0xA8, 0x3C, 255)  # type 5, drawn apart

    # docked: no timeline call on the following polls
    await coord.async_refresh()
    await coord.async_refresh()
    assert len(acc.timeline_calls) == 1


async def test_a_new_clean_starts_a_new_map_and_follows_the_timeline(hass: HomeAssistant) -> None:
    coord = await _setup(hass)
    acc = FakeAccount.instance
    assert coord.protocol.diagnostics()["cells"] == 64

    # the robot leaves the dock; RealTimeMapStart is not re-reported yet
    acc.state["WorkMode"] = 6
    acc.state["RealTimeMap"] = {"MapData": _cells((1, 1, 4)), "CleanTime": 3}
    await coord.async_refresh()
    assert coord.protocol.diagnostics()["cells"] == 1  # the previous clean is gone

    # then it is, and the posts of the new clean come from the timeline
    start = START + 3_600_000
    acc.state["RealTimeMapStart"] = start // 1000
    acc.timeline["RealTimeMap"] += [
        (start + 10, {"MapData": _cells((1, 1, 4), (2, 1, 4)), "CleanTime": 5}),
        (start + 20, {"MapData": _cells((3, 1, 4), (4, 1, 2)), "CleanTime": 9}),
    ]
    with patch.object(FakeAccount, "_iot", _reported_at(start)):
        await coord.async_refresh()
        assert acc.timeline_calls[-1]["start"] == start
        assert coord.protocol.diagnostics()["cell_types"] == {4: 3, 2: 1}

        # incrementally, from the last post seen
        acc.timeline["RealTimeMap"].append((start + 30, {"MapData": _cells((5, 1, 4)),
                                                         "CleanTime": 12}))
        await coord.async_refresh()
        assert acc.timeline_calls[-1]["start"] == start + 21
        assert coord.protocol.diagnostics()["cells"] == 5

        # back on the dock: one closing read, the map stays
        acc.state["WorkMode"] = 9
        await coord.async_refresh()
        calls = len(acc.timeline_calls)
        await coord.async_refresh()
        await coord.async_refresh()
        assert len(acc.timeline_calls) == calls
        assert coord.protocol.diagnostics()["cells"] == 5
        assert hass.states.get("camera.robot_map").state != "unavailable"


async def test_stale_map_start_keeps_only_the_latest_clean(hass: HomeAssistant) -> None:
    """When RealTimeMapStart was not re-reported for a new clean, newest-first paging
    stops once CleanTime goes back instead of walking days of old posts."""
    def _two_cleans(self, *args, **kwargs):
        self.state = {"WorkMode": 9, "BatteryState": 80, "RealTimeMapStart": START // 1000,
                      "RealTimeMap": {"MapData": _cells((35, 30, 4)),
                                      "CurrentPiont": (35 << 16) | 30, "CleanTime": 40},
                      "ChargerPoint": {"Piont": (1 << 16) | 1, "DisplaySwitch": 1}}
        yesterday = [(START + i, {"MapData": _cells((i % 20, i // 20, 4)), "CleanTime": i * 4})
                     for i in range(400)]
        today = [(START + 500_000 + i, {"MapData": _cells((30 + i, 30, 4)), "CleanTime": 20 + i * 4})
                 for i in range(6)]
        self.timeline = {"RealTimeMap": yesterday + today}
        self.timeline_calls = []
        FakeAccount.instance = self

    with patch.object(FakeAccount, "__init__", _two_cleans):
        coord = await _setup(hass)
    acc = FakeAccount.instance
    assert len(acc.timeline_calls) == 1  # stopped after the newest page, not 3 pages
    assert coord.protocol.diagnostics()["cells"] == 6
    assert coord.protocol.diagnostics()["timeline_posts"] == 6


def _reported_at(start):
    """FakeAccount._iot with the properties' report time moved to `start`."""
    plain = FakeAccount._iot

    def _iot(self, path, d, api_ver="1.0.2"):
        r = plain(self, path, d, api_ver)
        if path == "/thing/properties/get":
            for v in r["data"].values():
                v["time"] = start
        return r

    return _iot
