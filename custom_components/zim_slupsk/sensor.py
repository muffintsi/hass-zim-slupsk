from datetime import datetime, timedelta
import logging
import re

from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval
from .gtfs_parser import GTFSParser
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def sanitize_name(name):
    """Zamienia polskie znaki i spacje na podkreślenia dla entity_id."""
    name = name.lower()
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w\d_]", "", name)  # Usuwamy znaki specjalne
    return name

async def async_setup_entry(hass, entry, async_add_entities):
    """Konfiguracja sensorów dla linii autobusowych."""
    _LOGGER.debug("Rozpoczynanie konfiguracji sensorów...")

    stop_id = entry.data["stop_id"]
    parser = hass.data[DOMAIN]

    _LOGGER.debug(f"Stop ID: {stop_id}")

    # Pobierz dane o przystanku z parsera
    stop_data = parser.data["stops"].get(stop_id, {})
    stop_name = stop_data.get("stop_name", "Nieznany")
    stop_code = stop_data.get("stop_code", "")
    stop_code_fragment = stop_code.split("/")[-1] if "/" in stop_code else stop_code

    try:
        stop_lat = float(stop_data.get("stop_lat", 0.0))
        stop_lon = float(stop_data.get("stop_lon", 0.0))
    except ValueError:
        _LOGGER.error(f"Niepoprawny format współrzędnych dla przystanku {stop_id}")
        stop_lat = 0.0
        stop_lon = 0.0

    # Pełna nazwa przystanku
    stop_full_name = f"{stop_name} {stop_code_fragment}"
    stop_full_id = sanitize_name(stop_full_name)

    # Pobierz odjazdy dla przystanku
    departures_by_line = parser.get_next_departures(stop_id)
    if not departures_by_line:
        _LOGGER.warning(f"⚠️ Brak odjazdów (dla wszystkich linii) na przystanku {stop_full_name}.")
        return

    sensors = []
    for line, dep_list in departures_by_line.items():
        # Tworzymy 1 sensor per linia
        entity_id = f"sensor.linia_{line}_{stop_full_id}"
        # Przekazujemy dep_list do konstruktora
        sensor = BusSensor(
            parser,
            stop_full_name,
            stop_full_id,
            line,
            entry.entry_id,
            entity_id,
            stop_lat,
            stop_lon,
            stop_name,
            stop_code
        )
        sensors.append(sensor)

    if not sensors:
        _LOGGER.warning(f"⚠️ Nie utworzono sensorów – departures_by_line={departures_by_line}")
        return

    _LOGGER.info(f"✅ Utworzono {len(sensors)} sensorów dla przystanku {stop_full_name}: {[s.name for s in sensors]}")
    async_add_entities(sensors, True)

    async def update_sensors(_):
        _LOGGER.info(f"🔄 Odświeżanie sensorów dla przystanku {stop_full_name}")
        updated = parser.get_next_departures(stop_id)
        if not updated:
            _LOGGER.warning(f"⚠️ Brak nowych odjazdów (dla wszystkich linii) dla {stop_full_name}.")
            return

        # Dla każdej encji pobieramy nową listę kursów TEJ SAMEJ linii
        for sensor in sensors:
            if sensor.hass is None:
                _LOGGER.warning(f"⚠️ Sensor {sensor.name} nie jest jeszcze zarejestrowany.")
                continue

            line = sensor.line
            new_dep_list = updated.get(line, [])
            sensor.update_departures(new_dep_list)
            sensor.async_write_ha_state()

    # Ustawienie harmonogramu odświeżania sensorów co minutę
    async_track_time_interval(hass, update_sensors, timedelta(minutes=1))

    # **Początkowa aktualizacja sensorów**
    await update_sensors(None)

class BusSensor(Entity):
    """
    Sensor reprezentujący konkretną linię na danym przystanku.
    Stan -> najbliższy kurs
    nextbustime / nextbushour -> kolejny kurs
    bushour -> skrócona (HH:MM) wersja stanu
    + Nowość: latitude, longitude, stop_code
    """

    def __init__(
        self,
        parser: GTFSParser,
        stop_full_name: str,
        stop_full_id: str,
        line: str,
        entry_id: str,
        entity_id: str,
        stop_lat: float,
        stop_lon: float,
        stop_name: str,
        stop_code: str
    ):
        """Initialize the sensor."""
        self._parser = parser
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

        self.update_departures([])  # Initialize with empty list

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
            #_LOGGER.warning(f"⚠️ Linia {self._line} nie ma nadchodzących autobusów dla {self._stop_full_name}")
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

        _LOGGER.info(
            f"🚍 Sensor {self.name} (linia {self._line}) -> stan={self._state}, next={self._nextbustime}"
        )
        _LOGGER.debug(f"Departures for sensor {self.name}: {dep_list}")

    async def async_update(self):
        """Fetch new state data for the sensor."""
        # Aktualizacja zostanie wykonana przez update_sensors w async_setup_entry
        pass

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
    def state(self):
        """Najbliższy kurs w strefie lokalnej (ISO format)."""
        return self._state

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
            "stop_code": self._stop_code
        }
