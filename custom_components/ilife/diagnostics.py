"""Diagnostics for the ILIFE integration.

Home Assistant exposes a "Download diagnostics" button on the integration and on
each device. It dumps the account setup plus, for every vacuum, the raw property
payload the cloud returns (including the map fields). This is the data to attach
to a bug report when a map renders blank or a model behaves differently, since it
shows the actual keys and values the device reports.

Credentials and account/device identifiers are redacted before the file is
written, so it is safe to share.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_UID, DOMAIN

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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the whole account (every bound vacuum)."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    coordinators = (store.get("coordinators") or {}).values()
    return {
        "entry": {
            "backend": store.get("backend") or entry.data.get("backend"),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "device_count": len(coordinators),
        "devices": [_device_diag(c) for c in coordinators],
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
            return _device_diag(coordinator)
    return {"error": "device not found in this config entry"}
