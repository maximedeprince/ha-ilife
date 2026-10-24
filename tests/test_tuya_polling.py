"""How many calls the ILIFE Clean backend makes against the Tuya Cloud Project (#32).

A user's free Trial Edition ran out after polling every 30 s had spent the month's
allowance (about 26,000 calls) in about a week. These tests keep the pace under it, and
the quota error recognisable.

Pure functions, no Home Assistant: `python -m pytest tests/`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
_pkg = types.ModuleType("_ilife_polling")
_pkg.__path__ = [str(_PKG)]
sys.modules["_ilife_polling"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ilife_polling.{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
polling = _load("tuya_polling")
tuya_api = _load("tuya_api")

MONTH = 31


class TestBudget:
    def test_a_vacuum_that_cleans_an_hour_a_day_fits_the_trial_with_room_to_spare(self):
        assert polling.calls_per_day(1) * MONTH < 0.6 * polling.TRIAL_CALLS_PER_MONTH

    def test_three_hours_of_cleaning_a_day_still_fit(self):
        assert polling.calls_per_day(3) * MONTH < polling.TRIAL_CALLS_PER_MONTH

    def test_two_vacuums_on_one_project_fit_together(self):
        assert 2 * polling.calls_per_day(1, vacuums=2) * MONTH < polling.TRIAL_CALLS_PER_MONTH

    def test_the_old_pace_did_not(self):
        # 30-second status reads alone, before #32
        assert 24 * 3600 / 30 * MONTH > polling.TRIAL_CALLS_PER_MONTH


class TestPace:
    @pytest.mark.parametrize("status", [
        {"status": "smart"}, {"status": "cleaning"}, {"status": "goto_charge"},
        {"status": "zone_clean"}, {"power_go": True},
    ])
    def test_busy(self, status):
        assert polling.busy(status)

    @pytest.mark.parametrize("status", [
        {"status": "charging"}, {"status": "charge_done"}, {"status": "standby"},
        {"status": "sleep"}, {"status": "paused"}, {"status": "base_charging"},
        {"status": "fully_charged_in_dock"}, {"power_go": True, "pause": True}, {},
    ])
    def test_not_busy(self, status):
        assert not polling.busy(status)

    def test_a_busy_vacuum_is_followed_closely(self):
        assert polling.poll_interval(True, 1, now=0, follow_up_until=0) == polling.ACTIVE_INTERVAL

    def test_a_command_is_followed_closely_for_a_while(self):
        assert polling.poll_interval(False, 1, now=10, follow_up_until=60) == polling.ACTIVE_INTERVAL
        assert polling.poll_interval(False, 1, now=61, follow_up_until=60) == polling.IDLE_INTERVAL

    def test_vacuums_on_one_project_share_the_idle_pace(self):
        assert polling.poll_interval(False, 3, now=0, follow_up_until=0) == 3 * polling.IDLE_INTERVAL


class TestQuotaError:
    QUOTA = {"success": False, "code": 28841004, "t": None,
             "msg": "No permissions. Your quota of Trial Edition is used up."}

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(tuya_api, "_do", lambda *args: self.QUOTA)
        tuya = tuya_api.TuyaClient("access-id", "access-secret", "uid")
        tuya._token, tuya._token_expiry = "token", float("inf")
        return tuya

    def test_it_is_its_own_error_and_says_what_happened(self, client):
        with pytest.raises(tuya_api.TuyaQuotaError) as caught:
            client.device_detail("vacuum-device-id-0001")
        assert "26,000" in str(caught.value)
        # not a setup mistake: no reauthentication flow for it
        assert not isinstance(caught.value, tuya_api.TuyaAuthError)

    def test_even_on_the_token_request(self, client):
        client._token = None
        with pytest.raises(tuya_api.TuyaQuotaError):
            client.authenticate()

    def test_calls_are_counted_by_endpoint_without_ids(self, client):
        for _ in range(2):
            with pytest.raises(tuya_api.TuyaError):
                client.device_detail("bf0123456789abcdefgh")
        with pytest.raises(tuya_api.TuyaError):
            client.stored_map_records("bf0123456789abcdefgh")
        usage = client.usage()
        assert usage["calls"] == 3
        assert usage["by_endpoint"] == {
            "GET /v1.0/devices/{id}": 2,
            "GET /v1.0/users/sweepers/file/{id}/list": 1,
        }
        assert "bf0123456789abcdefgh" not in str(usage)
