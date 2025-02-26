from datetime import datetime, timedelta, date
import logging
import re

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util
from .gtfs_parser import GTFSParser
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Setup calendar platform for zim_slupsk."""
    parser = hass.data[DOMAIN]

    _LOGGER.info("🔄 Rejestracja kalendarzy dla przystanku...")

    # Pobierz przystanek ID z konfiguracji
    stop_id = entry.data["stop_id"]
    stop_info = parser.data["stops"].get(stop_id, {})
    stop_name = stop_info.get("stop_name", "Nieznany")
    stop_code = stop_info.get("stop_code", "")
    stop_lat = float(stop_info.get("stop_lat", 0.0))
    stop_lon = float(stop_info.get("stop_lon", 0.0))
    stop_code_fragment = stop_code.split("/")[-1] if "/" in stop_code else stop_code

    # Pełna nazwa przystanku
    stop_full_name = f"{stop_name} {stop_code_fragment}"
    _LOGGER.debug(f"Przystanek: {stop_full_name}, Latitude: {stop_lat}, Longitude: {stop_lon}")

    # Pobieramy odjazdy dla przystanku asynchronicznie, aby uniknąć blokowania setupu
    departures_by_line = await hass.async_add_executor_job(parser.get_next_departures, stop_id)
    if not departures_by_line:
        _LOGGER.warning(f"⚠️ Brak odjazdów (dla wszystkich linii) na przystanku {stop_full_name}.")
        return

    calendars = []
    for line in departures_by_line.keys():
        entity_id = f"calendar.linia_{line}_{sanitize_name(stop_full_name)}"
        calendar = CalendarSensor(
            parser,
            stop_id,  # Przekazujemy stop_id bezpośrednio
            stop_full_name,
            line,
            entry.entry_id,
            entity_id,
            stop_lat,  # Przekazujemy rzeczywiste wartości
            stop_lon,
            stop_name,
            stop_code
        )
        calendars.append(calendar)

    if not calendars:
        _LOGGER.warning(f"⚠️ Nie utworzono kalendarzy dla przystanku {stop_full_name}.")
        return

    _LOGGER.info(f"✅ Utworzono {len(calendars)} kalendarzy dla przystanku {stop_full_name}.")
    async_add_entities(calendars, True)

def sanitize_name(name):
    """Zamienia polskie znaki i spacje na podkreślenia dla entity_id."""
    name = name.lower()
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w\d_]", "", name)  # Usuwamy znaki specjalne
    return name

def parse_datetime(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        # Upewnij się, że datetime jest świadomy strefy czasowej
        if dt.tzinfo is None:
            dt = dt_util.as_utc(dt)
        return dt
    except ValueError as e:
        _LOGGER.error(f"Niepoprawny format daty: {dt_str} -> {e}")
        return None

class CalendarSensor(CalendarEntity):
    """
    Sensor typu Calendar dla danej linii i przystanku zawierający godziny odjazdów przez 7 dni.
    """

    def __init__(
        self,
        parser: GTFSParser,
        stop_id: str,  # Dodano stop_id
        stop_full_name: str,
        line: str,
        entry_id: str,
        entity_id: str,
        stop_lat: float,
        stop_lon: float,
        stop_name: str,
        stop_code: str
    ):
        self._parser = parser
        self._stop_id = stop_id  # Przechowujemy stop_id
        self._stop_full_name = stop_full_name
        self._stop_full_id = sanitize_name(stop_full_name)
        self._line = line
        self._entry_id = entry_id
        self.entity_id = entity_id

        # Atrybuty do wyświetlenia w kalendarzu:
        self._stop_lat = stop_lat
        self._stop_lon = stop_lon
        self._stop_name = stop_name
        self._stop_code = stop_code

        self._events = []
        # Usunięto wywołanie update_departures() z __init__
        # Ciężkie operacje będą wykonywane asynchronicznie w async_added_to_hass()

    async def async_added_to_hass(self):
        """Wykonuje operacje po dodaniu encji do Home Assistant."""
        _LOGGER.debug(f"Adding {self.name} to hass and setting up update interval.")
        # Harmonogram aktualizacji co minutę
        async_track_time_interval(self.hass, self.async_update_event_list, timedelta(minutes=1))
        # Natychmiastowa aktualizacja w tle
        self.hass.async_create_task(self.async_update_event_list())

    async def async_update_event_list(self, _now=None):
        """Aktualizuje listę wydarzeń i odświeża stan encji."""
        _LOGGER.debug(f"Updating event list for {self.name}.")
        # Przenosimy synchronizacyjną operację update_departures do osobnego wątku
        await self.hass.async_add_executor_job(self.update_departures)
        self.async_write_ha_state()

    @property
    def name(self):
        """Nazwa sensora."""
        return f"Linia {self._line} - {self._stop_full_name}"

    @property
    def unique_id(self):
        return f"zim_slupsk_calendar_{self._line}_{self._stop_full_id}_{self._entry_id}"

    @property
    def icon(self):
        """Ikona kalendarza (mdi:calendar)."""
        return "mdi:calendar"

    @property
    def event(self):
        """
        Bieżące wydarzenie kalendarza.
        """
        if self._events:
            event_data = self._events[0]  # Pierwsze wydarzenie w posortowanej liście
            _LOGGER.debug(f"Przetwarzanie wydarzenia: {event_data}")  # Dodatkowe logowanie
            try:
                event_start = datetime.fromisoformat(event_data["start"])
                event_end = datetime.fromisoformat(event_data["end"])
            except ValueError as e:
                _LOGGER.error(f"Niepoprawny format daty w wydarzeniu: {e}")
                return None

            return CalendarEvent(
                summary=event_data["title"],
                start=event_start,
                end=event_end,
                description=event_data["description"]
            )
        return None

    async def async_get_events(self, hass, start_date, end_date):
        """
        Zwraca listę wydarzeń między start_date a end_date.
        """
        _LOGGER.debug(f"Received start_date: {start_date} ({type(start_date)}), end_date: {end_date} ({type(end_date)})")

        # Konwersja start_date i end_date na datetime.date, jeśli są typu datetime.datetime
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        if isinstance(end_date, datetime):
            end_date = end_date.date()

        events = []
        for event_data in self._events:
            try:
                # Zachowujemy datetime.datetime dla CalendarEvent
                event_start = datetime.fromisoformat(event_data["start"])
                event_end = datetime.fromisoformat(event_data["end"])
            except ValueError as e:
                _LOGGER.error(f"Niepoprawny format daty w wydarzeniu: {e}")
                continue

            event_start_date = event_start.date()
            event_end_date = event_end.date()

            _LOGGER.debug(f"Comparing event_start_date: {event_start_date} with start_date: {start_date} and end_date: {end_date}")

            # Porównanie dat
            if start_date <= event_start_date <= end_date:
                event = CalendarEvent(
                    summary=event_data["title"],
                    start=event_start,
                    end=event_end,
                    description=event_data["description"]
                )
                events.append(event)
        return events

    def update_departures(self):
        """
        Aktualizuje listę wydarzeń na następne 7 dni.
        """
        self._events = self._parser.get_departures_for_week(self._line, stop_id=self._stop_id)
        _LOGGER.info(
            f"📅 Calendar {self.name} (linia {self._line}) -> liczba wydarzeń: {len(self._events)}"
        )
        # Usunięto wywołanie async_write_ha_state() tutaj

    @property
    def extra_state_attributes(self):
        """Atrybuty dodatkowe dla kalendarza."""
        return {
            "line": self._line,
            "latitude": self._stop_lat,
            "longitude": self._stop_lon,
            "stop_name": self._stop_name,
            "stop_code": self._stop_code,
            "friendly_name": self.name
        }

    @property
    def state(self):
        """Stan sensora: liczba wydarzeń."""
        return f"{len(self._events)} wydarzeń"

    def update(self):
        """HA wywołuje update -> i tak mamy update_departures()"""
        self.update_departures()
