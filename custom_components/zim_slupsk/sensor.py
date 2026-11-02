"""Bus sensors updated in one batch per configured stop."""

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .calendar import sanitize_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    parser = coordinator.store.parser
    stop = parser.get_stops()[coordinator.stop_id]
    name = stop.get("stop_name", "Nieznany")
    code = stop.get("stop_code", "")
    full_name = f"{name} {code.split('/')[-1]}"
    full_id = sanitize_name(full_name)
    try:
        lat, lon = float(stop.get("stop_lat") or 0), float(stop.get("stop_lon") or 0)
    except (TypeError, ValueError):
        lat = lon = 0.0
    lines = await hass.async_add_executor_job(parser.get_lines, coordinator.stop_id)
    async_add_entities(
        [
            BusSensor(
                coordinator,
                full_name,
                full_id,
                line,
                entry.entry_id,
                f"sensor.linia_{line}_{full_id}",
                lat,
                lon,
                name,
                code,
            )
            for line in lines
        ]
    )


class BusSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor reprezentujący konkretną linię na danym przystanku.
    Stan -> najbliższy kurs
    nextbustime / nextbushour -> kolejny kurs
    bushour -> skrócona (HH:MM) wersja stanu
    + Nowość: latitude, longitude, stop_code
    """

    def __init__(
        self,
        coordinator,
        stop_full_name: str,
        stop_full_id: str,
        line: str,
        entry_id: str,
        entity_id: str,
        stop_lat: float,
        stop_lon: float,
        stop_name: str,
        stop_code: str,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._parser = coordinator.store.parser
        self._stop_full_name = stop_full_name
        self._stop_full_id = stop_full_id
        self._line = line
        self._entry_id = entry_id
        self.entity_id = entity_id

        # Atrybuty do wyświetlenia w sensorze:
        self._stop_lat = stop_lat
        self._stop_lon = stop_lon
        self._stop_name = stop_name
        self._stop_code = stop_code

        self._state = None
        self._bushour = None
        self._nextbustime = None
        self._nextbushour = None
        self._direction_first = None
        self._direction_second = None

        self.update_departures((coordinator.data or {}).get("departures", {}).get(line, []))

    @property
    def line(self):
        """Zwraca numer linii."""
        return self._line

    def update_departures(self, dep_list: list):
        """
        dep_list -> do 2 elementów: [ {line, datetime, departure_time, direction}, { ... }, ]
        [0] -> stan = najbliższy kurs
        [1] -> nextbustime/nextbushour = kolejny
        """
        if not dep_list:
            # _LOGGER.warning(f"⚠️ Linia {self._line} nie ma nadchodzących autobusów dla {self._stop_full_name}")
            self._state = None
            self._bushour = None
            self._nextbustime = None
            self._nextbushour = None
            self._direction_first = None
            self._direction_second = None
            return

        # PIERWSZY kurs
        first = dep_list[0]
        dt_first = first["datetime"]
        local_dt_first = dt_util.as_local(dt_first)

        # Stan -> ISO
        self._state = local_dt_first.isoformat()
        # bushour -> HH:MM
        self._bushour = local_dt_first.strftime("%H:%M")
        # direction
        self._direction_first = first.get("direction", "").capitalize()

        # DRUGI kurs
        if len(dep_list) >= 2:
            second = dep_list[1]
            dt_second = second["datetime"]
            local_dt_second = dt_util.as_local(dt_second)
            self._nextbustime = local_dt_second.strftime("%Y-%m-%d %H:%M")
            self._nextbushour = local_dt_second.strftime("%H:%M")
            self._direction_second = second.get("direction", "").capitalize()
        else:
            self._nextbustime = None
            self._nextbushour = None
            self._direction_second = None

        _LOGGER.debug(
            f"🚍 Sensor {self.name} (linia {self._line}) -> stan={self._state}, next={self._nextbustime}"
        )
        _LOGGER.debug(f"Departures for sensor {self.name}: {dep_list}")

    @callback
    def _handle_coordinator_update(self):
        self.update_departures(
            (self.coordinator.data or {}).get("departures", {}).get(self._line, [])
        )
        super()._handle_coordinator_update()

    @property
    def name(self):
        """Nazwa sensora."""
        return f"Linia {self._line} - {self._stop_full_name}"

    @property
    def unique_id(self):
        return f"zim_slupsk_{self._line}_{self._stop_full_id}_{self._entry_id}"

    @property
    def icon(self):
        """Ikona autobusu (mdi:bus)."""
        return "mdi:bus"

    @property
    def native_value(self):
        """Najbliższy kurs w strefie lokalnej (ISO format)."""
        return datetime.fromisoformat(self._state) if self._state else None

    @property
    def device_class(self):
        """Klasa urządzenia jako timestamp."""
        return "timestamp"

    @property
    def extra_state_attributes(self):
        """Atrybuty dodatkowe: bushour, nextbustime, nextbushour, direction, latitude, longitude, stop_code."""
        return {
            "line": self._line,
            "Direction": self._direction_first,
            "BusHour": self._bushour,
            "NextBusDirection": self._direction_second,
            "NextBusTime": self._nextbustime,
            "NextBusHour": self._nextbushour,
            # NOWOŚĆ:
            "latitude": self._stop_lat,
            "longitude": self._stop_lon,
            "stop_name": self._stop_name,
            "stop_code": self._stop_code,
        }
