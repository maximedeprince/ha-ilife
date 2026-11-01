"""Re-auth policy of ILifeAccount._iot (#34).

A rejected/expired iotToken must re-login once and the call be replayed; a device-offline
(9201) or a transient 5xx must do neither. This matters because the robot drops its cloud
link while asleep at the dock and answers 9201 to every on-demand command — the old code
did a full OpenAccount re-login on *any* non-200, so each command to a sleeping robot
forced a login, which is the very thing the account's risk-control throttles.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types

_PKG_DIR = pathlib.Path(
    os.environ.get("ILIFE_PKG")
    or pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
)

# api.py does `from .brands import ...`, so load both as members of a synthetic package
# rather than importing the real custom_components.ilife (its __init__ pulls in HA).
_PKG = "ilife_under_test"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules[_PKG] = _pkg
    for _name in ("brands", "api"):
        _spec = importlib.util.spec_from_file_location(f"{_PKG}.{_name}", _PKG_DIR / f"{_name}.py")
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = _mod
        _spec.loader.exec_module(_mod)

api = sys.modules[f"{_PKG}.api"]


def _account(monkeypatch, replies):
    """An ILifeAccount whose signed-call layer returns `replies` in order, counting the
    number of network calls and of re-logins."""
    acc = api.ILifeAccount("e@x.tld", "pw", region="eu", brand="ilife")
    acc.token = "T0"
    calls = {"post": 0, "login": 0}
    sent = iter(replies)

    def fake_post(brand, host, path, d, token=None, api_ver="1.0.2"):
        calls["post"] += 1
        return next(sent)

    def fake_login():
        calls["login"] += 1
        acc.token = "T%d" % calls["login"]
        return []

    monkeypatch.setattr(api, "_post_iot", fake_post)
    monkeypatch.setattr(acc, "login", fake_login)
    return acc, calls


def test_token_rejection_relogins_once_then_replays(monkeypatch):
    acc, calls = _account(monkeypatch, [{"code": 401}, {"code": 200, "data": {"ok": 1}}])
    r = acc._iot("/thing/properties/get", {"iotId": "x"})
    assert r["code"] == 200
    assert calls == {"post": 2, "login": 1}


def test_device_offline_does_not_relogin(monkeypatch):
    acc, calls = _account(monkeypatch, [{"code": api.OFFLINE_CODE}])
    r = acc._iot("/thing/properties/set", {"iotId": "x"})
    assert r["code"] == api.OFFLINE_CODE
    assert calls == {"post": 1, "login": 0}


def test_transient_5xx_does_not_relogin(monkeypatch):
    acc, calls = _account(monkeypatch, [{"httperror": 502, "body": "bad gateway"}])
    r = acc._iot("/thing/properties/set", {"iotId": "x"})
    assert r["httperror"] == 502
    assert calls == {"post": 1, "login": 0}


def test_rate_limit_does_not_relogin(monkeypatch):
    acc, calls = _account(monkeypatch, [{"httperror": 429}])
    acc._iot("/thing/properties/get", {"iotId": "x"})
    assert calls == {"post": 1, "login": 0}


def test_offline_after_reauth_is_returned_without_a_second_login(monkeypatch):
    # token rejected -> re-login -> the replayed call finds the robot asleep: return the
    # 9201, and crucially do not loop into a second login.
    acc, calls = _account(monkeypatch, [{"code": 401}, {"code": api.OFFLINE_CODE}])
    r = acc._iot("/thing/properties/set", {"iotId": "x"})
    assert r["code"] == api.OFFLINE_CODE
    assert calls == {"post": 2, "login": 1}


def test_success_makes_no_extra_call(monkeypatch):
    acc, calls = _account(monkeypatch, [{"code": 200, "data": {}}])
    acc._iot("/thing/properties/get", {"iotId": "x"})
    assert calls == {"post": 1, "login": 0}
