"""Re-auth policy of ILifeAccount._iot, and network failures in _do (#34).

A rejected iotToken must re-login once and the call be replayed; a device-offline (9201)
or a transient 429/5xx must do neither. This matters because the robot drops its cloud
link while asleep at the dock and answers 9201 to every on-demand command — the old code
did a full OpenAccount re-login on *any* non-200, so each command to a sleeping robot
forced a login, which is the very thing the account's risk-control throttles.
"""
from __future__ import annotations

import http.client
import importlib.util
import os
import pathlib
import sys
import types

import pytest

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
    """An ILifeAccount whose signed-call layer answers with `replies` in order (an
    exception in the list is raised instead), counting network calls and re-logins."""
    acc = api.ILifeAccount("e@x.tld", "pw", region="eu", brand="ilife")
    acc.token = "T0"
    calls = {"post": 0, "login": 0}
    sent = iter(replies)

    def fake_post(brand, host, path, d, token=None, api_ver="1.0.2"):
        calls["post"] += 1
        reply = next(sent)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def fake_login():
        calls["login"] += 1
        acc.token = "T%d" % calls["login"]
        return []

    monkeypatch.setattr(api, "_post_iot", fake_post)
    monkeypatch.setattr(acc, "login", fake_login)
    return acc, calls


# 29003 "identityId is blank" is what the cloud answers for a token it can't resolve
# (checked live against eu-central-1); 401 is the gateway's generic "request auth error".
@pytest.mark.parametrize("code", [29003, "29003", 401])
def test_token_rejection_relogins_once_then_replays(monkeypatch, code):
    acc, calls = _account(monkeypatch, [{"code": code}, {"code": 200, "data": {"ok": 1}}])
    r = acc._iot("/thing/properties/get", {"iotId": "x"})
    assert r["code"] == 200
    assert calls == {"post": 2, "login": 1}


@pytest.mark.parametrize("code", [api.OFFLINE_CODE, str(api.OFFLINE_CODE)])
def test_device_offline_does_not_relogin(monkeypatch, code):
    acc, calls = _account(monkeypatch, [{"code": code}])
    r = acc._iot("/thing/properties/set", {"iotId": "x"})
    assert r["code"] == code
    assert calls == {"post": 1, "login": 0}


@pytest.mark.parametrize("reply", [
    {"httperror": 502, "body": "bad gateway"},   # non-JSON error page
    {"httperror": 429},
    {"code": 503, "message": "service not available"},  # JSON error body
    {"code": 500, "message": "service error"},
    {"code": 429, "message": "too many requests"},
])
def test_transient_failure_neither_relogins_nor_replays(monkeypatch, reply):
    acc, calls = _account(monkeypatch, [reply])
    assert acc._iot("/thing/properties/set", {"iotId": "x"}) == reply
    assert calls == {"post": 1, "login": 0}


def test_network_error_propagates_without_relogin(monkeypatch):
    acc, calls = _account(monkeypatch, [api.ILifeError("network error: timed out")])
    with pytest.raises(api.ILifeError):
        acc._iot("/thing/properties/set", {"iotId": "x"})
    assert calls == {"post": 1, "login": 0}


def test_offline_after_reauth_is_returned_without_a_second_login(monkeypatch):
    # token rejected -> re-login -> the replayed call finds the robot asleep: return the
    # 9201, and crucially do not loop into a second login.
    acc, calls = _account(monkeypatch, [{"code": 29003}, {"code": api.OFFLINE_CODE}])
    r = acc._iot("/thing/properties/set", {"iotId": "x"})
    assert r["code"] == api.OFFLINE_CODE
    assert calls == {"post": 2, "login": 1}


def test_command_to_a_sleeping_robot_is_offline_without_any_login(monkeypatch):
    # The user-facing case: Start pressed while the vacuum sleeps at its dock. It must
    # surface as ILifeOfflineError (the card's "device offline" message), login-free.
    acc, calls = _account(monkeypatch, [{"code": api.OFFLINE_CODE}])
    vacuum = api.ILifeDevice(acc, {"iotId": "x"})
    with pytest.raises(api.ILifeOfflineError):
        vacuum.work_mode(2)
    assert calls == {"post": 1, "login": 0}


def test_success_makes_no_extra_call(monkeypatch):
    acc, calls = _account(monkeypatch, [{"code": 200, "data": {}}])
    acc._iot("/thing/properties/get", {"iotId": "x"})
    assert calls == {"post": 1, "login": 0}


class _FailingResponse:
    """What urlopen hands back when the connection dies while the reply is read."""

    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        raise self._exc


@pytest.mark.parametrize("phase", ["connect", "read"])
@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b""),
    ConnectionResetError(104, "Connection reset by peer"),
])
def test_network_failure_is_an_ilife_error(monkeypatch, phase, exc):
    # Not a URLError: before, these escaped _do as raw exceptions, which HA's setup
    # does not retry (an ILifeError becomes ConfigEntryNotReady, which it does).
    def fake_urlopen(req, timeout=None):
        if phase == "connect":
            raise exc
        return _FailingResponse(exc)

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(api.ILifeError, match="network error"):
        api._do("example.invalid", "/x", {}, b"{}")
