"""Regression tests for the ILIFE Clean (Tuya) command derivation.

The fixtures are the `/specifications` responses two users attached to issues #23 and
#24 — real devices, not invented schemas. Both answered Tuya error 2008 ("command or
value not support") to `vacuum.start` because the integration sent a DP neither of them
advertises. The rule these tests enforce is the one that was broken:

    every command sent to a device must name a DP that device advertises as writable,
    and carry a value that DP accepts.

Pure functions, no Home Assistant needed: `python -m pytest tests/` or `python tests/…`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

# Loaded straight from their files, under a throwaway package name: importing
# custom_components.ilife would run its __init__.py, which needs a full Home Assistant.
# const.py and tuya_dynamic.py deliberately have no HA imports, and these tests are part
# of what keeps it that way.
_PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ilife"
_pkg = types.ModuleType("_ilife")
_pkg.__path__ = [str(_PKG)]
sys.modules["_ilife"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_ilife.{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
tuya_dynamic = _load("tuya_dynamic")

command_for = tuya_dynamic.command_for
enum_command = tuya_dynamic.enum_command
parse_functions = tuya_dynamic.parse_functions
range_values = tuya_dynamic.range_values
unknown_functions = tuya_dynamic.unknown_functions

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    d = json.loads((FIXTURES / f"{name}.json").read_text())
    return parse_functions(d["specification"]), d["status"]


def commands(functions):
    """Exactly what TuyaVacuum.__init__ derives, kept in step with vacuum.py."""
    return {
        "start": (command_for(functions, "power_go", "start", "smart", "clean", boolean=True)
                  or command_for(functions, "switch", "start", "smart", "clean", boolean=True)),
        "stop": (command_for(functions, "power_go", "stop", "standby", "idle", boolean=False)
                 or command_for(functions, "switch", "stop", "standby", "idle", boolean=False)),
        "pause": (command_for(functions, "pause", "pause", boolean=True)
                  or enum_command(functions, "power_go", "pause")),
        "return_to_base": (command_for(functions, "switch_charge", "chargego", "charge",
                                       boolean=True)
                           or enum_command(functions, "mode", "chargego", "charge_go",
                                           "back_charge")),
        "locate": command_for(functions, "seek", "seek", boolean=True),
    }


# --- the two devices that reported the bug ------------------------------------ #

def test_boolean_power_go_models_start_and_stop_on_power_go():
    """#23 (A30 Pro) and #24 (T20s): `power_go` is a Boolean and `switch` does not
    exist, so both commands must land on `power_go` — the old code sent `switch`."""
    for model in ("ilife_t20s", "ilife_a30_pro"):
        functions, _ = load(model)
        assert functions["power_go"]["type"] == "Boolean", model
        assert "switch" not in functions, model
        cmds = commands(functions)
        assert cmds["start"] == ("power_go", True), model
        assert cmds["stop"] == ("power_go", False), model


def test_every_command_is_writable_on_the_device():
    for model in ("ilife_t20s", "ilife_a30_pro"):
        functions, _ = load(model)
        for action, cmd in commands(functions).items():
            assert cmd is not None, f"{model}: no command for {action}"
            code, value = cmd
            assert code in functions, f"{model}: {action} targets unadvertised DP {code!r}"
            legal = range_values(functions, code)
            assert not legal or value in legal, f"{model}: {action}={value!r} not in {legal}"


def test_pause_never_degrades_into_stop():
    """A Boolean `power_go` must not be used as a pause fallback: false means stop."""
    for model in ("ilife_t20s", "ilife_a30_pro"):
        functions, _ = load(model)
        assert commands(functions)["pause"] == ("pause", True), model


def test_suction_and_cistern_are_dedicated_not_generic():
    """Suction becomes the vacuum's fan_speed and cistern the water-level select, so
    neither may still show up as a nameless generic entity."""
    for model in ("ilife_t20s", "ilife_a30_pro"):
        functions, status = load(model)
        assert range_values(functions, "suction") == ["strong", "normal", "gentle"], model
        assert range_values(functions, "cistern") == ["low", "middle", "high"], model
        spec = json.loads((FIXTURES / f"{model}.json").read_text())["specification"]
        generic = unknown_functions(spec, status, "Enum")
        assert "suction" not in generic and "cistern" not in generic, model


# --- shapes other models use, so the fix stays general ------------------------ #

def test_enum_power_go_still_works():
    functions = {"power_go": {"type": "Enum",
                              "values": {"range": ["start", "pause", "stop"]}}}
    assert command_for(functions, "power_go", "start", "smart", boolean=True) == \
        ("power_go", "start")
    assert command_for(functions, "power_go", "stop", boolean=False) == ("power_go", "stop")
    assert enum_command(functions, "power_go", "pause") == ("power_go", "pause")


def test_boolean_switch_only_model():
    functions = {"switch": {"type": "Boolean", "values": {}}}
    assert commands(functions)["start"] == ("switch", True)
    assert commands(functions)["stop"] == ("switch", False)


def test_missing_dp_yields_no_command_instead_of_a_wrong_one():
    assert command_for({}, "power_go", "start") is None
    assert enum_command({}, "mode", "chargego") is None
    # an Enum that simply has no matching value must not fall through to a Boolean
    functions = {"power_go": {"type": "Enum", "values": {"range": ["standby"]}}}
    assert command_for(functions, "power_go", "start", "smart", boolean=True) is None


def test_return_home_falls_back_to_the_mode_enum():
    functions = {"mode": {"type": "Enum", "values": {"range": ["smart", "chargego"]}}}
    assert commands(functions)["return_to_base"] == ("mode", "chargego")


def test_match_is_case_insensitive_and_keeps_the_devices_own_spelling():
    functions = {"mode": {"type": "Enum", "values": {"range": ["Smart", "ChargeGo"]}}}
    assert enum_command(functions, "mode", "chargego") == ("mode", "ChargeGo")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
