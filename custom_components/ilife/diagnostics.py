"""Diagnostics for the ILIFE integration.

Home Assistant exposes a "Download diagnostics" button on the integration and on
each device. It dumps the account setup plus, for every vacuum, the raw property
payload the cloud returns (including the map fields). This is the data to attach
to a bug report when a map renders blank or a model behaves differently, since it
shows the actual keys and values the device reports.

Credentials and account/device identifiers are redacted before the file is
written, so it is safe to share.

On ILIFE Clean the dump also probes Tuya's separate realtime-map API, because
"the map is blank" is otherwise indistinguishable from "this account cannot reach
the map API at all". It reports what came back — sizes, checksums, the 24-byte
header, and what the decoder made of it — but never the map body itself: that is
the home's floor plan, and this file is routinely attached to public issues.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_UID, DOMAIN
from .tuya_api import TuyaError
from .tuya_map import decode_tuya_map

# Keys whose values are removed from the dump wherever they appear (recursively).
# Credentials plus the identifiers that tie the account/device to a real person.
#
# Both backends are covered. On ILIFE Clean the config entry holds the Tuya Cloud
# Project credentials and the device list carries `local_key`, which is enough to
# control the vacuum locally — a diagnostics file attached to a public issue must
# not contain any of it.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "email",
    "password",
    "iotId",
    "deviceName",
    "identityId",
    "identity_id",
    "token",
    "iotToken",
    "sessionId",
    # ILIFE Clean (Tuya)
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_UID,
    "local_key",
    "owner_id",
    "id",
    "uuid",
    "ip",
    "lat",
    "lon",
    "sn",
}


def _device_diag(coordinator: Any) -> dict[str, Any]:
    """One vacuum's dump: identity, live status and the raw property payload.

    Both backends' coordinators land here and they do not carry the same
    attributes: the ILIFEHOME one accumulates a clean history, the ILIFE Clean
    (Tuya) one carries the device's DP specification instead. Read what is
    actually there rather than assuming one shape — assuming `history` made the
    whole download fail with an AttributeError on every Tuya device (#3).
    """
    diag: dict[str, Any] = {
        "device": async_redact_data(coordinator.api.device, TO_REDACT),
        "online": coordinator.online,
        # The full state as returned by the cloud, with the accumulated live map
        # merged in. This is where a blank-map or unknown-model issue shows up.
        "state": async_redact_data(coordinator.data or {}, TO_REDACT),
    }
    # ILIFEHOME only: the parsed clean history behind the map archive.
    history = getattr(coordinator, "history", None)
    if history is not None:
        diag["history_count"] = len(history)
    # ILIFE Clean only: the DP specification lists every code the vacuum
    # advertises, including those the integration has no dedicated mapping for.
    # This is what answers "does this model expose map data at all?".
    spec = getattr(coordinator, "spec", None)
    if spec:
        diag["specification"] = async_redact_data(spec, TO_REDACT)
    return diag


def _describe_map_file(item: dict[str, Any]) -> dict[str, Any]:
    """Describe one map file without reproducing it.

    The floor plan lives in the LZ4 block after byte 24. The header in front of it
    does not describe a home, only the grid: version, id, size, resolution and dock
    position. That is what decides whether this model's map is supported, so it is
    the part worth shipping — alongside what the decoder actually managed to read.
    """
    payload = item.get("payload") or b""
    described = {
        "map_type": item.get("map_type"),
        "role": item.get("role"),
        "source": item.get("source"),
        "record_id": item.get("record_id"),
        "record_time": item.get("record_time"),
        "filename": item.get("filename"),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "header_hex": payload[:24].hex(),
    }
    if item.get("map_type") != 0:
        return described
    try:
        decoded = decode_tuya_map(payload)
    except Exception as err:  # noqa: BLE001
        # The whole point of the probe: name what the decoder choked on, by type,
        # so an unsupported map version reads as such instead of as a blank card.
        described["decode"] = {
            "success": False,
            "error": f"{type(err).__name__}: {err}",
        }
        return described
    header = decoded["header"]
    described["decode"] = {
        "success": True,
        "map_version": header["version"],
        "map_id": header["map_id"],
        "width": header["width"],
        "height": header["height"],
        "resolution_cm": header["resolution_cm"],
        "room_count": len(decoded["rooms"]),
        "room_areas_m2": decoded["room_areas_m2"],
        "path_points": len(decoded["path_points"]),
        "virtual_walls": len(decoded["virtual_walls"]),
        "no_go_zones": len(decoded["no_go_zones"]),
        "embedded_commands": decoded["embedded_commands"],
    }
    return described


async def _async_device_diag(
    hass: HomeAssistant, coordinator: Any
) -> dict[str, Any]:
    """Build one device dump and probe Tuya's separate realtime-map API on demand."""
    diag = _device_diag(coordinator)
    fetch = getattr(coordinator.api, "realtime_map_files", None)
    if fetch is None:
        return diag

    try:
        files = await hass.async_add_executor_job(fetch)
    except TuyaError as err:
        diag["realtime_map_probe"] = {"success": False, "error": str(err)}
        return diag
    except Exception as err:  # noqa: BLE001
        diag["realtime_map_probe"] = {
            "success": False,
            "error": f"unexpected map probe error: {err}",
        }
        return diag

    described = [_describe_map_file(item) for item in files]
    diag["realtime_map_probe"] = {
        "success": True,
        "note": (
            "The map body is deliberately not included: it is the home's floor plan "
            "and this file is meant to be shareable. What is here says whether the "
            "map arrived and whether the decoder understood it."
        ),
        "files": described,
    }
    return diag


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the whole account (every bound vacuum)."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinators = (store.get("coordinators") or {}).values()
    devices = []
    for coordinator in coordinators:
        devices.append(await _async_device_diag(hass, coordinator))
    return {
        "entry": {
            "backend": store.get("backend") or entry.data.get("backend"),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "device_count": len(coordinators),
        "devices": devices,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single vacuum."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinators = store.get("coordinators") or {}
    ids = {ident for (dom, ident) in device.identifiers if dom == DOMAIN}
    for device_key, coordinator in coordinators.items():
        if device_key in ids:
            return await _async_device_diag(hass, coordinator)
    return {"error": "device not found in this config entry"}
