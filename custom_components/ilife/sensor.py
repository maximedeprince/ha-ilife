"""ILIFE sensors: battery, consumable wear, last clean, history."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfTime

from .const import CLEANING_MODES, DOMAIN
from .entity import ILifeEntity


def _parts(state, field):
    return (state.get("PartsStatus") or {}).get(field)


def _rtm(state, field):
    return (state.get("RealTimeMap") or {}).get(field)


def _cleaning(state):
    return state.get("WorkMode") in CLEANING_MODES


def _hist(state, field):
    return (state.get("CleanHistory") or {}).get(field)


def _last_clean(state):
    ts = _hist(state, "StartTime") or state.get("CleanHistoryStartTime")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


# (translation_key, icon, unit, state_class, extractor, device_class)
SENSORS = [
    ("battery", "mdi:battery", PERCENTAGE, SensorStateClass.MEASUREMENT,
     lambda s: s.get("BatteryState"), SensorDeviceClass.BATTERY),
    ("main_brush", "mdi:brush", PERCENTAGE, SensorStateClass.MEASUREMENT,
     lambda s: _parts(s, "MainBrushLife"), None),
    ("side_brush", "mdi:brush-variant", PERCENTAGE, SensorStateClass.MEASUREMENT,
     lambda s: _parts(s, "SideBrushLife"), None),
    ("filter", "mdi:air-filter", PERCENTAGE, SensorStateClass.MEASUREMENT,
     lambda s: _parts(s, "FilterLife"), None),
    ("current_area", "mdi:vector-square", UnitOfArea.SQUARE_METERS, SensorStateClass.MEASUREMENT,
     lambda s: round((_rtm(s, "CleanArea") or 0) / 100, 1) if _cleaning(s) else None, None),
    ("current_time", "mdi:timer-play-outline", UnitOfTime.MINUTES, SensorStateClass.MEASUREMENT,
     lambda s: round((_rtm(s, "CleanTime") or 0) / 60) if _cleaning(s) else None, None),
    ("last_area", "mdi:ruler-square", UnitOfArea.SQUARE_METERS, None,
     lambda s: round((_hist(s, "CleanTotalArea") or 0) / 100, 1) or None, None),
    ("last_duration", "mdi:timer-outline", UnitOfTime.MINUTES, None,
     lambda s: round((_hist(s, "CleanTotalTime") or 0) / 60) or None, None),
    ("last_clean", "mdi:calendar-check", None, None,
     _last_clean, SensorDeviceClass.TIMESTAMP),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for coordinator in data["coordinators"].values():
        entities += [ILifeSensor(coordinator, *s) for s in SENSORS]
        entities.append(ILifeHistorySensor(coordinator))
    async_add_entities(entities)


class ILifeSensor(ILifeEntity, SensorEntity):
    def __init__(self, coordinator, key, icon, unit, state_class, extractor, device_class):
        super().__init__(coordinator)
        self._extractor = extractor
        self._attr_translation_key = key
        self._attr_unique_id = f"{self.api.iot_id}_{key}"
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        self._attr_device_class = device_class

    @property
    def native_value(self):
        try:
            return self._extractor(self.coordinator.data or {})
        except Exception:  # noqa: BLE001
            return None


class ILifeHistorySensor(ILifeEntity, SensorEntity):
    """Cleaning history: state = count, attribute 'cleans' = detailed list."""

    _attr_translation_key = "history"
    _attr_icon = "mdi:history"
    # do NOT store the map blobs in the recorder (~28 KB otherwise)
    _unrecorded_attributes = frozenset({"cleans"})

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.api.iot_id}_history"

    @property
    def native_value(self):
        return len(self.coordinator.history or [])

    @property
    def extra_state_attributes(self):
        return {"cleans": self.coordinator.history or []}
