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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ILifeAccount, ILifeAuthError, ILifeDevice, ILifeError
from .brands import DEFAULT_BRAND
from .const import (
    BACKEND_ILIFE_CLEAN,
    BACKEND_ILIFEHOME,
    CLEANING_MODES,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_BACKEND,
    CONF_BRAND,
    CONF_UID,
    DEFAULT_START_MODE,
    DOMAIN,
    TUYA_CATEGORY_VACUUM,
    TUYA_CONSUMABLES,
    TUYA_DP_DUST_FREQUENCY,
    TUYA_DP_RESET_MAP,
    TUYA_DP_STATUS,
    TUYA_DP_VOLUME,
    TUYA_STATUS_CLEANING,
)
from .tuya_api import (
    TuyaAuthError,
    TuyaClient,
    TuyaError,
    TuyaVacuum,
    parse_record_extend,
)
from .tuya_dynamic import parse_functions, range_values
from .tuya_map import clean_map_path, clean_map_thumbnail
from .tuya_rooms import CLEAN_ROOMS_CODE, parse_room_names
from .tuya_schedule import parse_schedules

_LOGGER = logging.getLogger(__name__)

ILIFEHOME_PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TIME,
    Platform.CAMERA,
]
ILIFE_CLEAN_PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.CAMERA,
]
PLATFORMS = ILIFEHOME_PLATFORMS

# ILIFE Clean cleaning history: how many stored maps (one per clean) to list.
HISTORY_SIZE = 10

CARD_URL = "/ilife_cards/ilife-vacuum-card.js"
CARD_FILENAME = "ilife-vacuum-card.js"


def _is_ilife_card_resource(url: str) -> bool:
    """Recognize both current and legacy/manual ILIFE card resource URLs."""
    base = str(url or "").split("?", 1)[0].rstrip("/")
    return (
        base == CARD_URL
        or base.endswith("/ilife-vacuum-card.js")
        or base.startswith("/ilife_cards/ilife-vacuum-card-")
    )


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


class ILifeTuyaCoordinator(DataUpdateCoordinator):
    """Polls one ILIFE Clean (Tuya) vacuum's status: {dp_code: value}."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: TuyaVacuum) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_{device.device_id}",
            update_interval=timedelta(seconds=30), config_entry=entry,
        )
        self.api = device
        self.online: bool | None = None
        self.spec: dict = {}
        self.spec_functions: dict = {}
        self._spec_loaded = False
        # Product DPs outside the standard instruction set (see tuya_rooms). They
        # change only when the map is edited in the app, so they are read slowly.
        self.model: dict[str, dict] = {}
        self.properties: dict[str, Any] = {}
        self.room_names: dict[int, str] = {}
        self._properties_next = 0.0
        # Cleaning history: one stored map per clean (A30 Pro), newest first, in the
        # card's "cleans" shape; start, area and duration come from each record.
        self.history: list[dict] = []
        self._history_cache: dict[str, dict] = {}
        # Not during setup: listing the maps downloads up to HISTORY_SIZE files.
        self._history_next = _time.monotonic() + 30
        self._history_fast_until = 0.0
        self._was_cleaning = False

    @property
    def supports_room_clean(self) -> bool:
        return CLEAN_ROOMS_CODE in self.properties

    @property
    def schedules(self) -> dict[int, dict]:
        return parse_schedules(self.properties)

    def _watch_run_end(self, status: dict[str, Any]) -> None:
        """A finished run is stored within a minute or so: look for it right away,
        then every minute for a while, instead of on the idle 5-minute cadence."""
        cleaning = str(status.get(TUYA_DP_STATUS) or "").lower() in TUYA_STATUS_CLEANING
        if self._was_cleaning and not cleaning:
            self._history_next = 0.0
            self._history_fast_until = _time.monotonic() + 600
        self._was_cleaning = cleaning

    async def _async_update_history(self) -> None:
        now = _time.monotonic()
        if now < self._history_next:
            return
        self._history_next = now + (60 if now < self._history_fast_until else 300)
        try:
            records = await self.hass.async_add_executor_job(
                self.api.stored_map_records, HISTORY_SIZE)
        except TuyaError:
            _LOGGER.debug("ILIFE Clean history unavailable", exc_info=True)
            return
        history = []
        for record in records:
            record_id = str(record["id"])
            entry = self._history_cache.get(record_id)
            if entry is None:
                entry = await self._async_history_entry(record)
            history.append(entry)
        self._history_cache = {
            entry["record_id"]: entry for entry in history if not entry.get("retry")
        }
        self.history = history

    async def _async_history_entry(self, record: dict) -> dict:
        """One history row with its map. A map that cannot be fetched right now is
        retried next cycle; one that cannot be decoded is kept without a picture."""
        record_id = str(record["id"])
        try:
            entry = self._history_entry(record)
        except Exception:  # noqa: BLE001 — history is optional, never fail the poll
            _LOGGER.debug("ILIFE Clean history record %s unreadable", record_id,
                          exc_info=True)
            entry = {"record_id": record_id, "start": 0, "area": None,
                     "duration": None, "thumb": None, "path": []}
        try:
            files = await self.hass.async_add_executor_job(
                self.api.stored_map_files, record)
        except TuyaError:
            _LOGGER.debug("ILIFE Clean history map %s unavailable", record_id,
                          exc_info=True)
            entry["retry"] = True
            return entry
        layout = next((f["payload"] for f in files if f.get("map_type") == 0), None)
        if layout:
            try:
                entry["thumb"] = await self.hass.async_add_executor_job(
                    clean_map_thumbnail, layout)
                entry["path"] = await self.hass.async_add_executor_job(
                    clean_map_path, layout)
            except Exception:  # noqa: BLE001 — e.g. a map version we cannot decode
                _LOGGER.debug("ILIFE Clean history map %s not decoded", record_id,
                              exc_info=True)
        return entry

    @staticmethod
    def _history_entry(record: dict) -> dict:
        """One card history row from a stored map record."""
        clean = parse_record_extend(record.get("extend"))
        if clean:
            # The robot writes its own wall-clock time: read it in HA's time zone.
            started = clean["started"].replace(tzinfo=dt_util.get_default_time_zone())
            start = int(started.timestamp())
        else:
            try:
                start = int(record.get("time") or 0)
            except (TypeError, ValueError):
                start = 0
        return {
            "record_id": str(record["id"]),
            "start": start,
            "area": clean["area"] if clean else None,
            "duration": clean["duration"] if clean else None,
            "thumb": None,
            "path": [],
        }

    def enum_range(self, code: str) -> list:
        """Every value `code` accepts: the thing model's range, else the spec's."""
        return (self.model.get(code) or {}).get("range") or range_values(
            self.spec_functions, code)

    async def async_write(self, code: str, value) -> None:
        """Write one DP by the standard command when the spec covers it, else through
        the thing model (e.g. suction "closed", which /specifications leaves out)."""
        spec_range = range_values(self.spec_functions, code)
        if code in self.spec_functions and (not spec_range or value in spec_range):
            await self.hass.async_add_executor_job(self.api.send, code, value)
        else:
            await self.hass.async_add_executor_job(self.api.issue_properties, {code: value})
            self.properties[code] = value
            self._properties_next = 0.0
        await self.async_request_refresh()

    async def _async_update_properties(self) -> None:
        if _time.monotonic() < self._properties_next:
            return
        self._properties_next = _time.monotonic() + 300
        try:
            self.properties = await self.hass.async_add_executor_job(self.api.properties)
        except (TuyaError, AttributeError, TypeError):
            _LOGGER.debug("ILIFE Clean product properties unavailable", exc_info=True)
            return
        try:
            self.room_names = parse_room_names(self.properties)
        except Exception:  # noqa: BLE001 — names are cosmetic, keep the old ones
            _LOGGER.debug("ILIFE Clean room names unreadable", exc_info=True)

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._spec_loaded:
            try:
                self.spec = await self.hass.async_add_executor_job(self.api.specification)
                self.spec_functions = parse_functions(self.spec)
            except TuyaError:
                _LOGGER.debug("ILIFE Clean specification unavailable", exc_info=True)
                self.spec = {}
            try:
                self.model = await self.hass.async_add_executor_job(self.api.model)
            except (TuyaError, AttributeError, TypeError):
                _LOGGER.debug("ILIFE Clean thing model unavailable", exc_info=True)
            self._spec_loaded = True
        await self._async_update_properties()
        try:
            detail = await self.hass.async_add_executor_job(self.api.detail)
        except TuyaError as err:
            raise UpdateFailed(str(err)) from err
        self.online = detail.get("online")
        status = {s["code"]: s.get("value") for s in detail.get("status") or [] if s.get("code")}
        self._watch_run_end(status)
        try:
            await self._async_update_history()
        except Exception:  # noqa: BLE001 — the history must never fail the status poll
            _LOGGER.debug("ILIFE Clean history update failed", exc_info=True)
        return status


async def _async_setup_ilifehome_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
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
        "backend": BACKEND_ILIFEHOME, "account": account, "coordinators": coordinators,
        "platforms": ILIFEHOME_PLATFORMS,
    }


def _async_remove_replaced_generic_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinators: dict
) -> None:
    """Drop the generic entities that dedicated ones replaced in 0.7.0.

    Consumable life used to be a raw minutes sensor, volume and dust-collection
    frequency read-only sensors, and the resets switches. Their replacements have
    another unique_id or domain, so the old ones would linger as "no longer
    provided" forever.
    """
    registry = er.async_get(hass)
    sensors = [*TUYA_CONSUMABLES, TUYA_DP_VOLUME, TUYA_DP_DUST_FREQUENCY]
    switches = [TUYA_DP_RESET_MAP, *(reset for _, reset, _, _ in TUYA_CONSUMABLES.values())]
    for device_id in coordinators:
        for domain, codes in (("sensor", sensors), ("switch", switches)):
            for code in codes:
                entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{device_id}_{code}")
                if entity_id:
                    entity = registry.async_get(entity_id)
                    if entity and entity.config_entry_id == entry.entry_id:
                        _LOGGER.info("ILIFE Clean: removing %s, replaced in 0.7.0", entity_id)
                        registry.async_remove(entity_id)


async def _async_setup_ilife_clean_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    client = TuyaClient(
        entry.data[CONF_ACCESS_ID], entry.data[CONF_ACCESS_SECRET],
        entry.data[CONF_UID], entry.data.get(CONF_REGION, "eu"),
    )
    try:
        devices = await hass.async_add_executor_job(client.list_devices)
    except TuyaAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TuyaError as err:
        raise ConfigEntryNotReady(str(err)) from err

    vacuums = [d for d in devices if d.get("category") == TUYA_CATEGORY_VACUUM]
    if not vacuums:
        # Rather than set up nothing at all, trust the account over our category guess:
        # a model filed under an unexpected category still deserves to work, and the
        # extra entities are visible enough that it gets reported.
        _LOGGER.warning(
            "ILIFE Clean: none of the %d linked Tuya devices are in the robot-vacuum "
            "category (%r); setting all of them up. Please report this with the "
            "integration's diagnostics: %s",
            len(devices), TUYA_CATEGORY_VACUUM,
            sorted({d.get("category") for d in devices}),
        )
        vacuums = devices
    elif len(vacuums) != len(devices):
        _LOGGER.debug("ILIFE Clean: %d of %d linked Tuya devices are vacuums",
                      len(vacuums), len(devices))

    coordinators: dict[str, ILifeTuyaCoordinator] = {}
    for dev in vacuums:
        coordinator = ILifeTuyaCoordinator(hass, entry, TuyaVacuum(client, dev))
        await coordinator.async_refresh()
        coordinators[dev["id"]] = coordinator
    if not coordinators:
        raise ConfigEntryNotReady("no device set up")
    _async_remove_replaced_generic_entities(hass, entry, coordinators)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "backend": BACKEND_ILIFE_CLEAN, "client": client, "coordinators": coordinators,
        "platforms": ILIFE_CLEAN_PLATFORMS,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_BACKEND) == BACKEND_ILIFE_CLEAN:
        await _async_setup_ilife_clean_entry(hass, entry)
    else:
        await _async_setup_ilifehome_entry(hass, entry)
    # Both backends ship the same card: registering it only on the ILIFEHOME path left
    # ILIFE Clean users with no "ILIFE Vacuum Card" in the card picker at all, and
    # copying the .js into www/community by hand as the only way out (#23).
    await _async_register_frontend(hass)
    platforms = hass.data[DOMAIN][entry.entry_id]["platforms"]
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    platforms = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get(
        "platforms", ILIFEHOME_PLATFORMS)
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
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
    if lovelace is None:
        return
    # Detect a storage-backed resource collection by what it can do, not by the name of
    # a mode attribute. `LovelaceData.mode` was renamed to `resource_mode` in Home
    # Assistant, and reading the old name through a getattr default meant this function
    # silently returned every single time: the card stopped being registered for anyone,
    # and installs that had it kept whatever URL version they were on (#23). Only
    # ResourceStorageCollection has async_create_item; the YAML one is read-only and is
    # the documented manual case.
    resources = getattr(lovelace, "resources", None)
    if resources is None or not hasattr(resources, "async_create_item"):
        _LOGGER.debug("ILIFE: Lovelace resources are not storage-backed; add %s manually",
                      CARD_URL)
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
                if _is_ilife_card_resource(r.get("url", ""))]
    try:
        if not existing:
            await resources.async_create_item({"res_type": "module", "url": want})
            _LOGGER.info("ILIFE: Lovelace card resource added (%s)", want)
        else:
            primary = existing[0]
            if primary.get("url") != want:
                await resources.async_update_item(
                    primary["id"], {"res_type": "module", "url": want}
                )
                _LOGGER.info("ILIFE: Lovelace card resource updated to %s", want)
            for duplicate in existing[1:]:
                if hasattr(resources, "async_delete_item"):
                    await resources.async_delete_item(duplicate["id"])
                elif duplicate.get("url") != want:
                    await resources.async_update_item(
                        duplicate["id"], {"res_type": "module", "url": want}
                    )
    except Exception:  # noqa: BLE001
        # Loud on purpose: a silent failure here leaves the user with no card in the
        # picker and nothing to go on, which is how #23 was spent.
        _LOGGER.warning(
            "ILIFE: could not register the Lovelace card resource automatically. Add it "
            "by hand under Settings > Dashboards > Resources: URL %s, type JavaScript "
            "module.", want, exc_info=True)
    finally:
        data["_card_registered"] = True
