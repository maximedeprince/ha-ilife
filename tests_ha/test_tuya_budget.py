"""The calls an ILIFE Clean vacuum makes against its Tuya Cloud Project, over a day (#32).

The integration is set up for real with the Tuya client replaced by one that counts the
calls, then the clock is moved through a day: docked, one hour of cleaning, docked. The
free Trial Edition allows about 26,000 calls a month; the old pace spent that in a week.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import json
import pathlib
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.ilife.const import DOMAIN
from custom_components.ilife.tuya_api import TuyaQuotaError
from custom_components.ilife.tuya_polling import TRIAL_CALLS_PER_MONTH

A30_PRO = json.loads(
    (pathlib.Path(__file__).parents[1] / "tests" / "fixtures" / "ilife_a30_pro.json").read_text()
)
DEVICE = {"id": "bf0123456789abcdefgh", "category": "sd", "name": "Robot",
          "product_name": "ILIFE A30 Pro", "online": True}


class FakeClient:
    """Stands in for TuyaClient: answers like an A30 Pro and counts every call."""

    instance: FakeClient

    def __init__(self, *args, **kwargs) -> None:
        self.calls: Counter[str] = Counter()
        self.status = dict(A30_PRO["status"])
        self.records = [{"id": "100", "time": 1_700_000_000, "extend": ""}]
        self.quota_spent = False
        self.refused = 0
        FakeClient.instance = self

    def _call(self, name: str) -> None:
        if self.quota_spent:
            self.refused += 1
            raise TuyaQuotaError("Tuya API error 28841004: quota used up", 28841004)
        self.calls[name] += 1

    def usage(self) -> dict:
        return {"calls": sum(self.calls.values())}

    def list_devices(self):
        self._call("list_devices")
        return [DEVICE]

    def device_detail(self, device_id):
        self._call("detail")
        return {"online": True,
                "status": [{"code": k, "value": v} for k, v in self.status.items()]}

    def device_specification(self, device_id):
        self._call("specification")
        return A30_PRO["specification"]

    def device_model(self, device_id):
        self._call("model")
        return {}

    def device_properties(self, device_id):
        self._call("properties")
        return {}

    def stored_map_records(self, device_id, count=5):
        self._call("map_list")
        return list(self.records)

    def latest_stored_map(self, device_id):
        records = self.stored_map_records(device_id)
        return records[0] if records else None

    def stored_map_files(self, device_id, record):
        self._call("map_download")
        return [{"map_type": 0, "payload": b"layout " + record["id"].encode()}]

    def realtime_map_files(self, device_id):
        self._call("realtime_map")
        return []

    def send_commands(self, device_id, commands):
        self._call("commands")
        return True

    def issue_properties(self, device_id, properties):
        self._call("issue_properties")


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
        # the card's resource: Lovelace is not loaded here, and it is not about the cloud
        patch("custom_components.ilife._async_register_frontend"),
        patch("custom_components.ilife.camera.render_tuya_map_png",
              return_value=(b"\x89PNG", {})),
        patch("custom_components.ilife.clean_map_thumbnail", return_value=None),
        patch("custom_components.ilife.clean_map_path", return_value=[]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield FakeClient.instance


async def _run(hass, freezer, minutes: float) -> None:
    # 5-second steps: a 30-second poll fires up to 5 s late, so the busy-hour counts
    # below come out a little under the real 120 an hour
    for _ in range(round(minutes * 12)):
        freezer.tick(timedelta(seconds=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_a_day_fits_the_free_trial(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, client: FakeClient
) -> None:
    await _run(hass, freezer, 60)  # the startup calls are behind us
    day = Counter()

    client.calls.clear()
    await _run(hass, freezer, 11 * 60)
    docked = client.calls.copy()
    day += docked

    client.calls.clear()
    client.status.update(status="smart", power_go=True)
    await _run(hass, freezer, 60)
    cleaning = client.calls.copy()
    day += cleaning

    client.calls.clear()
    # the run ends; its map is stored a few minutes later
    client.status.update(status="charging", power_go=False)
    await _run(hass, freezer, 5)
    client.records.insert(0, {"id": "101", "time": 1_700_100_000, "extend": ""})
    await _run(hass, freezer, 12 * 60 - 5)
    day += client.calls

    assert sum(day.values()) * 31 < 0.6 * TRIAL_CALLS_PER_MONTH, day
    # docked: a status read every 5 minutes, and little else
    assert 11 * 12 - 2 <= docked["detail"] <= 11 * 12 + 1, docked
    assert docked["realtime_map"] == 0, docked
    # cleaning: followed closely, the live map asked for a few times only
    assert cleaning["detail"] >= 90, cleaning
    assert 1 <= cleaning["realtime_map"] <= 3, cleaning
    # the new run's map was downloaded, once by the history and once by the camera
    assert day["map_download"] == 2, day


async def test_a_command_is_followed_closely(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, client: FakeClient
) -> None:
    await _run(hass, freezer, 10)
    client.calls.clear()
    await hass.services.async_call("vacuum", "locate",
                                   {"entity_id": "vacuum.robot"}, blocking=True)
    await _run(hass, freezer, 3)
    # the refresh right after the command, then every 30 s for COMMAND_FOLLOW_UP
    assert client.calls["detail"] >= 5
    await _run(hass, freezer, 1)
    client.calls.clear()
    await _run(hass, freezer, 20)
    # back to the idle pace once the follow-up is over
    assert client.calls["detail"] <= 5


async def test_a_spent_quota_is_reported_and_not_hammered(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, client: FakeClient
) -> None:
    await _run(hass, freezer, 10)
    client.quota_spent = True
    await _run(hass, freezer, 6)
    issue_id = f"tuya_quota_spent_{hass.config_entries.async_entries(DOMAIN)[0].entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert hass.states.get("vacuum.robot").state == "unavailable"

    client.refused = 0
    await _run(hass, freezer, 3 * 60)
    assert client.refused <= 4  # the status, once an hour; the map backs off on its own

    client.quota_spent = False
    await _run(hass, freezer, 61)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert hass.states.get("vacuum.robot").state != "unavailable"
