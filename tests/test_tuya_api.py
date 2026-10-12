"""What a refused Tuya call tells the user (#30).

The map endpoints belong to a Tuya service of their own. A permission error there
used to carry the setup hint ("IoT Core / Smart Home Basic Service"), which names
services the user already has, and to be raised as an authentication failure,
although everything but the map works without it.

Pure functions, no Home Assistant and no network: the transport is replaced.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
_pkg = types.ModuleType("_ilife_api")
_pkg.__path__ = [str(_PKG)]
sys.modules["_ilife_api"] = _pkg
_spec = importlib.util.spec_from_file_location("_ilife_api.tuya_api", _PKG / "tuya_api.py")
tuya_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tuya_api
_spec.loader.exec_module(tuya_api)

NOT_SUBSCRIBED = {"success": False, "code": 28841101,
                  "msg": "No permissions. This API is not subscribed."}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(tuya_api, "_do", lambda *args: NOT_SUBSCRIBED)
    tuya = tuya_api.TuyaClient("access-id", "access-secret", "uid")
    tuya._token, tuya._token_expiry = "token", float("inf")
    return tuya


def test_a_refused_map_endpoint_names_the_map_service(client):
    with pytest.raises(tuya_api.TuyaError) as caught:
        client.realtime_map_files("device")
    assert "Sweeping Robot Open Service" in str(caught.value)
    assert "Smart Home Basic Service" not in str(caught.value)
    # the rest of the integration works without the map service
    assert not isinstance(caught.value, tuya_api.TuyaAuthError)


def test_the_stored_map_endpoints_say_the_same(client):
    with pytest.raises(tuya_api.TuyaError) as caught:
        client.stored_map_records("device")
    assert "Sweeping Robot Open Service" in str(caught.value)


def test_other_endpoints_keep_the_setup_hint(client):
    with pytest.raises(tuya_api.TuyaAuthError) as caught:
        client.device_detail("device")
    assert "Smart Home Basic Service" in str(caught.value)
