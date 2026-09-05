"""ILIFE camera entity: renders the real-time map as a PNG."""
from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity import device_info
from .map import render_png


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(ILifeMapCamera(c) for c in data["coordinators"].values())


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
        return self.coordinator.last_update_success

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
