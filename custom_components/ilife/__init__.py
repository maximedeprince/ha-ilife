"""ILIFE Vacuum integration (Alibaba IoT cloud)."""
from __future__ import annotations

import logging
import time as _time  # aliased: a sibling submodule is named time.py (TimeEntity platform)
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ILifeAccount, ILifeAuthError, ILifeDevice, ILifeError
from .brands import DEFAULT_BRAND
from .const import CLEANING_MODES, CONF_BRAND, DEFAULT_START_MODE, DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TIME,
    Platform.CAMERA,
]

CARD_URL = "/ilife_cards/ilife-vacuum-card.js"
CARD_FILENAME = "ilife-vacuum-card.js"


class ILifeCoordinator(DataUpdateCoordinator):
    """Polls one vacuum's state; also refreshes online status, history and PNG archive."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: ILifeDevice) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_{device.iot_id}",
            update_interval=timedelta(seconds=30), config_entry=entry,
        )
        self.api = device
        self.clean_mode = DEFAULT_START_MODE
        self.history: list[dict] = []
        self.online: bool | None = None
        self._hist_next = 0.0
        # live map accumulation: RealTimeMap.MapData is only a small rolling window,
        # so we merge successive windows into the full current-clean map.
        self._rtmap_cells: dict[tuple[int, int], int] = {}
        self._rtmap_session: Any = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            state = await self.hass.async_add_executor_job(self.api.get_state)
        except ILifeError as err:
            raise UpdateFailed(str(err)) from err
        try:
            self.online = await self.hass.async_add_executor_job(self.api.is_online)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ILIFE online status unavailable", exc_info=True)
        if _time.monotonic() >= self._hist_next:
            self._hist_next = _time.monotonic() + 300
            try:
                self.history = await self.hass.async_add_executor_job(self.api.clean_history, 20)
                await self.hass.async_add_executor_job(self._archive_maps)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("ILIFE history unavailable", exc_info=True)
        self._merge_rtmap(state)
        return state

    def _merge_rtmap(self, state: dict) -> None:
        """While cleaning, accumulate the rolling RealTimeMap windows into the full
        map and inject it back so the camera renders the whole clean, not a fragment."""
        from .map import decode_cells, encode_cells

        rtm = state.get("RealTimeMap") or {}
        cleaning = state.get("WorkMode") in CLEANING_MODES
        if not cleaning:
            self._rtmap_session = None  # next clean starts a fresh map
            return
        md = rtm.get("MapData")
        if not md:
            return
        session = state.get("RealTimeMapStart")
        if session != self._rtmap_session:
            self._rtmap_session = session
            self._rtmap_cells = {}
        for x, y, t in decode_cells(md):
            self._rtmap_cells[(x, y)] = t
        merged = dict(rtm)
        merged["MapData"] = encode_cells(self._rtmap_cells)
        state["RealTimeMap"] = merged

    def _archive_maps(self) -> None:
        """Save each clean's map as a tiny PNG under www/ilife_maps/ (permanent gallery)."""
        import os

        from .map import render_clean_map_png

        folder = self.hass.config.path("www", "ilife_maps")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return
        for c in self.history or []:
            start, cm = c.get("start"), c.get("map")
            if not start or not cm:
                continue
            path = os.path.join(folder, f"{self.api.iot_id}_{start}.png")
            if os.path.exists(path):
                continue
            png = render_clean_map_png(cm)
            if png:
                try:
                    with open(path, "wb") as f:
                        f.write(png)
                except OSError:
                    pass


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    account = ILifeAccount(
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION, "eu"),
        entry.data.get(CONF_BRAND, DEFAULT_BRAND),
    )
    try:
        devices = await hass.async_add_executor_job(account.login)
    except ILifeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ILifeError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinators: dict[str, ILifeCoordinator] = {}
    for dev in devices:
        coordinator = ILifeCoordinator(hass, entry, ILifeDevice(account, dev))
        # per-device: one offline vacuum must not abort the whole account entry
        await coordinator.async_refresh()
        coordinators[dev["iotId"]] = coordinator
    if not coordinators:
        raise ConfigEntryNotReady("no device set up")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "account": account, "coordinators": coordinators,
    }
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # keep the '_card_*' flags; only drop DOMAIN when no entries remain
        if not any(isinstance(v, dict) and "coordinators" in v
                   for v in hass.data.get(DOMAIN, {}).values()):
            for k in ("_card_served", "_card_registered"):
                hass.data.get(DOMAIN, {}).pop(k, None)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a device from the UI only if it is gone from the account.

    A vacuum that was unbound (replaced, sold, reset) stays in the registry as an
    unavailable "ghost" device. This lets the user remove it. A device that is
    still bound is kept, since it would just be recreated on the next refresh.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    active = set((store.get("coordinators") or {}).keys())
    iot_ids = {ident for (dom, ident) in device.identifiers if dom == DOMAIN}
    return iot_ids.isdisjoint(active)


# --------------------------------------------------------------------------- #
#  Lovelace card auto-registration (safe: never wipes the user's resources)
# --------------------------------------------------------------------------- #
async def _async_register_frontend(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("_card_served"):
        try:
            from homeassistant.components.http import StaticPathConfig

            path = hass.config.path(f"custom_components/{DOMAIN}/www/{CARD_FILENAME}")
            await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, path, True)])
            data["_card_served"] = True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ILIFE: could not serve card static path", exc_info=True)

    if hass.state is CoreState.running:
        await _async_register_card_resource(hass)
    else:
        async def _on_started(_event):
            await _async_register_card_resource(hass)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_register_card_resource(hass: HomeAssistant) -> None:
    """Register the card as a Lovelace resource in storage mode.

    Guards against HA core issue #165767: writing before the resource collection is
    loaded rewrites .storage/lovelace_resources and would wipe ALL the user's resources.
    We only act once resources.loaded is True.
    """
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("_card_registered"):
        return
    lovelace = hass.data.get("lovelace")
    if lovelace is None or getattr(lovelace, "mode", None) != "storage":
        return  # YAML mode: resource is added manually (documented in README)
    resources = getattr(lovelace, "resources", None)
    if resources is None:
        return
    if not getattr(resources, "loaded", False):
        async def _retry(_now):
            await _async_register_card_resource(hass)
        async_call_later(hass, 5, _retry)
        return

    from homeassistant.loader import async_get_integration

    version = "0"
    try:
        version = (await async_get_integration(hass, DOMAIN)).version or "0"
    except Exception:  # noqa: BLE001
        pass
    want = f"{CARD_URL}?v={version}"
    existing = [r for r in resources.async_items()
                if str(r.get("url", "")).split("?")[0] == CARD_URL]
    try:
        if not existing:
            await resources.async_create_item({"res_type": "module", "url": want})
            _LOGGER.info("ILIFE: Lovelace card resource added")
        elif existing[0].get("url") != want:
            await resources.async_update_item(existing[0]["id"], {"res_type": "module", "url": want})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("ILIFE: could not register card resource", exc_info=True)
    finally:
        data["_card_registered"] = True
