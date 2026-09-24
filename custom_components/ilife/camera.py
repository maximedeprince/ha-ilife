"""ILIFE camera entity: renders the real-time map as a PNG."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BACKEND_ILIFE_CLEAN,
    DOMAIN,
    TUYA_DP_POWER_GO,
    TUYA_DP_REQUEST,
    TUYA_DP_STATUS,
    TUYA_DP_SWITCH,
    TUYA_STATUS_CLEANING,
)
from .entity import device_info
from .map import render_png
from .tuya_entity import tuya_device_info
from .tuya_dynamic import enum_command
from .tuya_map import render_tuya_map_png

_LOGGER = logging.getLogger(__name__)
TUYA_MAP_ACTIVE_CACHE_SECONDS = 30
TUYA_MAP_IDLE_CACHE_SECONDS = 300
TUYA_MAP_POST_CLEAN_SECONDS = 600
TUYA_MAP_REQUEST_SECONDS = 60
# Not every ILIFE Clean model publishes a map: the A30 Pro of #23 and the T20s of
# #24 both advertise `request`/`path_data` and never produce one, and a Cloud
# Project without the Robot Vacuum service never will either. Those are permanent
# conditions, so failures back off instead of retrying on every coordinator tick —
# otherwise the owners of those models get a cloud call and a traceback every 30s
# for a map that is never coming.
TUYA_MAP_RETRY_BACKOFF = (30, 60, 300, 900, 3600)


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    camera_class = (
        TuyaMapCamera if data.get("backend") == BACKEND_ILIFE_CLEAN else ILifeMapCamera
    )
    async_add_entities(camera_class(c) for c in data["coordinators"].values())


class ILifeMapCamera(CoordinatorEntity, Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "map"
    _attr_icon = "mdi:map"

    def __init__(self, coordinator):
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.api = coordinator.api
        self.content_type = "image/png"
        self._attr_unique_id = f"{self.api.iot_id}_map"
        self._attr_device_info = device_info(self.api)
        self._cache_key = None
        self._cache_png = None

    @property
    def available(self) -> bool:
        # A model that publishes no map should read as unavailable rather than as a
        # camera serving nothing; `map_last_error` says which of the two it is.
        return self.coordinator.last_update_success and self._cache_png is not None

    def _key(self, data):
        rtm = (data or {}).get("RealTimeMap") or {}
        rmd = (data or {}).get("RealMapData_1") or {}
        return (rtm.get("MapData"), rtm.get("CurrentPiont"), (data or {}).get("ChargerPiont"),
                rmd.get("UpdateTime"), rmd.get("MapData1"))

    async def async_camera_image(self, width=None, height=None):
        data = self.coordinator.data or {}
        key = self._key(data)
        if key != self._cache_key or self._cache_png is None:
            png = await self.hass.async_add_executor_job(render_png, data)
            if png is not None:
                self._cache_key = key
                self._cache_png = png
        return self._cache_png


class TuyaMapCamera(CoordinatorEntity, Camera):
    """Camera backed by Tuya's current/latest stored robot-vacuum map files."""

    _attr_has_entity_name = True
    _attr_translation_key = "map"
    _attr_icon = "mdi:map"

    def __init__(self, coordinator):
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.api = coordinator.api
        self.content_type = "image/png"
        self._attr_unique_id = f"{self.api.device_id}_map"
        self._attr_device_info = tuya_device_info(self.api)
        self._cache_png = None
        self._cache_metadata = {}
        self._cache_time = 0.0
        self._failures = 0
        self._refresh_lock = asyncio.Lock()
        self._refresh_task = None
        self._request_time = 0.0
        self._was_cleaning = self._is_cleaning()
        self._post_clean_until = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_background_refresh(force=True)

    async def async_will_remove_from_hass(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        await super().async_will_remove_from_hass()

    def _is_cleaning(self) -> bool:
        data = self.coordinator.data or {}
        status = str(data.get(TUYA_DP_STATUS) or "").lower()
        if status in TUYA_STATUS_CLEANING:
            return True
        # The V20 run flag is authoritative even if its firmware reports a new or
        # model-specific status value. Keep this aligned with TuyaVacuum.activity.
        return data.get(TUYA_DP_POWER_GO) is True or data.get(TUYA_DP_SWITCH) is True

    def _cache_seconds(self) -> int:
        if self._failures:
            index = min(self._failures, len(TUYA_MAP_RETRY_BACKOFF)) - 1
            return TUYA_MAP_RETRY_BACKOFF[index]
        if self._is_cleaning() or time.monotonic() < self._post_clean_until:
            return TUYA_MAP_ACTIVE_CACHE_SECONDS
        return TUYA_MAP_IDLE_CACHE_SECONDS

    def _handle_coordinator_update(self) -> None:
        cleaning = self._is_cleaning()
        force = False
        if cleaning != self._was_cleaning:
            self._cache_time = 0.0
            force = True
            # A new run means a new map worth fetching, so drop the backoff — but
            # only for a device that has ever produced one. On a model that never
            # will, resetting here would mean a fresh warning every single clean.
            if self._cache_png is not None:
                self._failures = 0
            if self._was_cleaning and not cleaning:
                self._post_clean_until = time.monotonic() + TUYA_MAP_POST_CLEAN_SECONDS
            elif cleaning:
                self._post_clean_until = 0.0
            self._was_cleaning = cleaning
        super()._handle_coordinator_update()
        # Do not make map freshness depend on whether a Lovelace card happens to be
        # visible. The coordinator already wakes every 30 seconds; use that heartbeat.
        self._schedule_background_refresh(force=force)

    def _schedule_background_refresh(self, *, force: bool = False) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = self.hass.async_create_task(
            self._async_refresh_map(force=force)
        )

    async def _async_request_current_map(self) -> None:
        """Ask models such as V20 to publish both current map and path data."""
        if not self._is_cleaning():
            return
        now = time.monotonic()
        if self._request_time and now - self._request_time < TUYA_MAP_REQUEST_SECONDS:
            return
        command = enum_command(
            self.coordinator.spec_functions, TUYA_DP_REQUEST, "get_both", "get_map"
        )
        if command is None:
            return
        self._request_time = now
        try:
            await self.hass.async_add_executor_job(self.api.send, *command)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ILIFE Clean map publication request failed", exc_info=True)

    @property
    def available(self) -> bool:
        # A model that publishes no map should read as unavailable rather than as a
        # camera serving nothing; `map_last_error` says which of the two it is.
        return self.coordinator.last_update_success and self._cache_png is not None

    @property
    def extra_state_attributes(self):
        return self._cache_metadata

    def _note_failure(self, reason: str) -> None:
        """Record a failed refresh, loudly the first time and quietly after that.

        The first failure is worth a warning: it is usually the Cloud Project
        missing the Robot Vacuum service, which is fixable and invisible otherwise.
        Repeats are not — on a model that has no map they would never stop.
        """
        self._failures += 1
        self._cache_metadata = {
            **self._cache_metadata,
            "map_last_error": reason,
            "map_fetched_at": int(time.time()),
        }
        if self._failures == 1:
            _LOGGER.warning(
                "ILIFE Clean map unavailable for %s: %s. If this model does publish a "
                "map, check that the Tuya Cloud Project has the Robot Vacuum Open APIs "
                "service authorized; retrying with backoff",
                self.api.device_id,
                reason,
            )
        else:
            _LOGGER.debug(
                "ILIFE Clean map still unavailable for %s (attempt %s): %s",
                self.api.device_id,
                self._failures,
                reason,
            )

    async def _async_refresh_map(self, *, force: bool = False) -> None:
        if not force and self._cache_time:
            if time.monotonic() - self._cache_time < self._cache_seconds():
                return
        async with self._refresh_lock:
            if not force and self._cache_time:
                if time.monotonic() - self._cache_time < self._cache_seconds():
                    return
            try:
                await self._async_request_current_map()
                files = await self.hass.async_add_executor_job(self.api.realtime_map_files)
                layout = next(
                    (item.get("payload") for item in files if item.get("map_type") == 0),
                    None,
                )
                path = next(
                    (item.get("payload") for item in files if item.get("map_type") == 1),
                    None,
                )
                if not layout:
                    self._note_failure("Tuya returned no layout map for this device")
                else:
                    png, metadata = await self.hass.async_add_executor_job(
                        render_tuya_map_png, layout, path
                    )
                    source = next(
                        (item for item in files if item.get("map_type") == 0), {}
                    )
                    metadata.update({
                        "map_source": source.get("source"),
                        "map_record_id": source.get("record_id"),
                        "map_record_time": source.get("record_time"),
                        "map_fetched_at": int(time.time()),
                        "map_payload_sha256": hashlib.sha256(layout).hexdigest(),
                    })
                    self._cache_png = png
                    self._cache_metadata = metadata
                    if self._failures:
                        _LOGGER.info("ILIFE Clean map recovered for %s", self.api.device_id)
                    self._failures = 0
            except Exception as err:  # noqa: BLE001
                self._note_failure(f"{type(err).__name__}: {err}")
            self._cache_time = time.monotonic()
            self.async_write_ha_state()

    async def async_camera_image(self, width=None, height=None):
        await self._async_refresh_map()
        return self._cache_png
