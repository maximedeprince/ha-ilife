"""W450 map in Home Assistant: rebuilt from the property timeline, like the app."""
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
W450 = "a1hTwV7eU4O"
START = 1793481147286


def _cells(*recs):
    return base64.b64encode(b"".join(struct.pack(">hhB", a, b, t) for a, b, t in recs)).decode()


def _road(*pts):
    return base64.b64encode(b"".join(struct.pack(">hh", a, b) for a, b in pts)).decode()


class FakeAccount:
    instance: "FakeAccount"

    def __init__(self, *args, **kwargs) -> None:
        FakeAccount._base_init(self)

    def _base_init(self) -> None:
        self.state = {"WorkMode": 6, "BatteryState": 80, "RealTimeMapStart": 1,
                      "RealTimeRoadStart": 1}
        self.timeline = {
            "RealTimeMap": [(START + 10, {"MapData": _cells(*[(a, b, 10) for a in range(5) for b in range(8)])}),
                            (START + 20, {"MapData": _cells(*[(5, b, 2) for b in range(8)])})],
            "RealMapRoadData": [(START + 15, {"RoadData": _road((0, 0), (0, 7), (4, 7))})],
        }
        self.timeline_calls: list[dict] = []
        FakeAccount.instance = self

    def login(self):
        return [{"iotId": IOT_ID, "productKey": W450, "nickName": "Robot", "productName": "ILIFE"}]

    def device_status(self):
        return {IOT_ID: True}

    def _iot(self, path, d, api_ver="1.0.2"):
        if path == "/thing/properties/get":
            self.polls = getattr(self, "polls", 0) + 1
            data = {k: {"value": v, "time": START} for k, v in self.state.items()}
            return {"code": 200, "data": data}
        if path == "/living/device/property/timeline/get" and d["identifier"] == "CleanHistory":
            hdr = struct.pack(">Bhhh", 1, 0, 0, 6)
            packs = [hdr + bytes([0, 10, 30]), hdr + bytes([0, 2, 6])]  # each pack repeats the header
            items = [{"timestamp": START + 100 + i, "data": json.dumps({
                "StartTime": 1794049498, "PackNum": 2, "PackId": i + 1, "CleanTotalArea": 2436,
                "CleanTotalTime": 1605, "StopCleanReason": 1,
                "CleanMapData": base64.b64encode(b).decode()})}
                for i, b in enumerate(packs)]
            return {"code": 200, "data": {"items": items}}
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


async def test_w450_map_is_rebuilt_from_the_timeline(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u@example.com", data={
        "backend": "ilifehome", "email": "u@example.com", "password": "pw",
        "region": "eu", "brand": "ilife"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    acc = FakeAccount.instance
    coord = next(iter(hass.data[DOMAIN][entry.entry_id]["coordinators"].values()))
    assert coord.protocol.key == "w450"
    # history fetched from RealTimeMapStart's report time
    assert acc.timeline_calls[0]["start"] == START
    diag = coord.protocol.diagnostics()
    assert diag["cells"] == 48 and diag["cell_types"] == {10: 40, 2: 8}

    # history: W450 packs re-encoded for the card / gallery (2bpp), not misread
    from custom_components.ilife.map import decode_clean_bitmap
    hist = coord.history[0]
    cells = decode_clean_bitmap(hist["map"])
    assert len(cells) == 36 and {t for *_xy, t in cells} == {1, 2}
    # docked card keeps the camera: calibration_points are exposed
    cam = hass.states.get("camera.robot_map")
    assert len(cam.attributes["calibration_points"]) == 3

    image = await async_get_image(hass, "camera.robot_map")
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    img = Image.open(io.BytesIO(image.content))
    assert img.width > img.height  # app orientation: 8 columns x 6 rows

    # new post while cleaning: fetched incrementally, from the last seen timestamp
    acc.timeline["RealTimeMap"].append((START + 30, {"MapData": _cells((9, 9, 11))}))
    n = len(acc.timeline_calls)
    await coord.async_refresh()
    assert acc.timeline_calls[n]["start"] == START + 21
    assert coord.protocol.diagnostics()["cells"] == 49

    # docked: no more timeline calls after the closing fetch
    acc.state["WorkMode"] = 9
    await coord.async_refresh()  # closing fetch (was active)
    await coord.async_refresh()
    calls_docked = len(acc.timeline_calls)
    await coord.async_refresh()
    await coord.async_refresh()
    assert len(acc.timeline_calls) == calls_docked
    assert hass.states.get("camera.robot_map").state != "unavailable"


async def test_w450_stale_map_start_shows_only_the_latest_clean(hass: HomeAssistant) -> None:
    """The robot does not always re-report RealTimeMapStart: yesterday's and today's
    posts then share one timeline; CleanTime going back marks today's start."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u@example.com", data={
        "backend": "ilifehome", "email": "u@example.com", "password": "pw",
        "region": "eu", "brand": "ilife"})
    entry.add_to_hass(hass)
    with patch.object(FakeAccount, "__init__", _two_days_init):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    acc = FakeAccount.instance
    coord = next(iter(hass.data[DOMAIN][entry.entry_id]["coordinators"].values()))
    diag = coord.protocol.diagnostics()
    assert diag["cells"] == 6 and diag["cell_types"] == {10: 6}  # today's only
    # newest-first read stopped at today's start: yesterday's 400 posts not all paged
    assert sum(1 for c in acc.timeline_calls if c["identifier"] == "RealTimeMap") == 1  # not 3
    road = [c for c in acc.timeline_calls if c["identifier"] == "RealMapRoadData"][0]
    assert road["start"] == START + 500_000

    # another clean starts, RealTimeMapStart still not re-reported: map restarts
    acc.timeline["RealTimeMap"].append((START + 900_000, {"MapData": _cells((50, 50, 11)),
                                                          "CleanTime": 4}))
    await coord.async_refresh()
    assert coord.protocol.diagnostics()["cells"] == 1


def _two_days_init(self, *args, **kwargs):
    FakeAccount._base_init(self)
    yesterday = [(START + i, {"MapData": _cells((i % 20, i // 20, 10)), "CleanTime": i * 4})
                 for i in range(400)]
    today = [(START + 500_000 + i, {"MapData": _cells((30 + i, 30, 10)), "CleanTime": 72 + i * 4})
             for i in range(6)]
    self.timeline = {"RealTimeMap": yesterday + today, "RealMapRoadData": []}
