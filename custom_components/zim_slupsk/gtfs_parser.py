import zipfile
import csv
import os
import aiofiles  # Importujemy aiofiles do asynchronicznego odczytu
import logging
from datetime import datetime, timedelta, date
from .const import GTFS_FILE_PATH

from homeassistant.util import dt as dt_util  # Importowanie dt_util dla świadomości strefy czasowej


_LOGGER = logging.getLogger(__name__)

class GTFSParser:
    def __init__(self):
        self.data = {}
        self.download_url = None
        self.stop_id_map = {}  # Mapa pełnej nazwy przystanku do stop_id

    async def load_data(self):
        """Wczytuje plik GTFS i parsuje stops.txt, stop_times.txt, trips.txt oraz opcjonalnie calendar i calendar_dates."""
        if not os.path.exists(GTFS_FILE_PATH):
            _LOGGER.error("Plik GTFS nie istnieje!")
            return False

        try:
            with zipfile.ZipFile(GTFS_FILE_PATH, 'r') as z:
                _LOGGER.info(f"📂 Pliki w GTFS: {z.namelist()}")

                required_files = ['stops.txt', 'stop_times.txt', 'trips.txt']
                if not all(file in z.namelist() for file in required_files):
                    _LOGGER.error("❌ Brak wymaganych plików w archiwum GTFS!")
                    return False

                with z.open('stops.txt') as f:
                    reader = csv.DictReader(f.read().decode('utf-8-sig').splitlines())
                    self.data["stops"] = {row["stop_id"]: row for row in reader}
                    # Tworzenie mapy stop_full_name -> stop_id
                    for stop_id, stop in self.data["stops"].items():
                        full_name = f"{stop.get('stop_name', 'Nieznany')} {stop.get('stop_code', '')}"
                        self.stop_id_map[full_name.strip()] = stop_id

                with z.open('stop_times.txt') as f:
                    reader = csv.DictReader(f.read().decode('utf-8-sig').splitlines())
                    self.data["stop_times"] = list(reader)

                with z.open('trips.txt') as f:
                    reader = csv.DictReader(f.read().decode('utf-8-sig').splitlines())
                    self.data["trips"] = {row["trip_id"]: row for row in reader}

                # Odczyt calendar.txt z lokalnego folderu asynchronicznie
                calendar_path = os.path.join(os.path.dirname(GTFS_FILE_PATH), "calendar.txt")
                if os.path.exists(calendar_path):
                    async with aiofiles.open(calendar_path, "r", encoding="utf-8-sig") as f:
                        contents = await f.read()
                    reader = csv.DictReader(contents.splitlines())

                    # PATCH: normalizacja pól, żeby uniknąć problemów z '\r', spacjami, BOM itp.
                    normalized_calendar = []
                    for row in reader:
                        row["service_id"] = (row.get("service_id") or "").strip()
                        row["start_date"] = (row.get("start_date") or "").strip()
                        row["end_date"] = (row.get("end_date") or "").strip()
                        # dni tygodnia też mogą mieć whitespace
                        for k in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
                            if k in row:
                                row[k] = (row.get(k) or "").strip()
                        normalized_calendar.append(row)

                    self.data["calendar"] = normalized_calendar
                else:
                    self.data["calendar"] = []
    

                # Opcjonalnie wczytujemy calendar_dates.txt
                # MINIMAL PATCH: szukamy też w podfolderach ZIP-a (np. gtfs/calendar_dates.txt)
                calendar_dates_member = next((name for name in z.namelist() if name.endswith('calendar_dates.txt')), None)

                if calendar_dates_member:
                    with z.open(calendar_dates_member) as f:
                        reader = csv.DictReader(f.read().decode('utf-8-sig').splitlines())

                        # PATCH: normalizacja pól, żeby porównania == działały zawsze
                        normalized_calendar_dates = []
                        for row in reader:
                            row["service_id"] = (row.get("service_id") or "").strip()
                            row["date"] = (row.get("date") or "").strip()
                            row["exception_type"] = (row.get("exception_type") or "").strip()
                            normalized_calendar_dates.append(row)

                        self.data["calendar_dates"] = normalized_calendar_dates
                else:
                    self.data["calendar_dates"] = []

            _LOGGER.info(f"✅ Wczytano {len(self.data['stops'])} przystanków.")
            return True
        except zipfile.BadZipFile:
            _LOGGER.error("❌ Plik GTFS nie jest prawidłowym archiwum ZIP!")
            return False
        except Exception as e:
            _LOGGER.error(f"❌ Błąd podczas wczytywania danych GTFS: {e}")
            return False

    def get_stops(self):
        """Zwraca listę dostępnych przystanków."""
        if "stops" not in self.data:
            _LOGGER.error("Brak danych o przystankach.")
            return {}

        stops = {
            stop_id: {
                "stop_name": stop.get("stop_name", "Nieznany"),
                "stop_code": stop.get("stop_code", "")
            }
            for stop_id, stop in self.data["stops"].items()
        }

        _LOGGER.info(f"🚌 Znalezione przystanki: {stops}")
        return stops

    # ------------------------------
    # Obsługa service_id (weekendy/święta)
    # ------------------------------
    def service_is_active_on_date(self, service_id, check_date: date) -> bool:
        """
        Zasada:
        - jeśli calendar_dates ma JAKIKOLWIEK wpis dla tej daty -> calendar_dates jest źródłem prawdy dla całego dnia
          (nie robimy fallbacku do calendar.txt)
        - jeśli calendar_dates nie ma żadnych wpisów dla tej daty -> fallback do calendar.txt
        """
        cdate_str = check_date.strftime("%Y%m%d")

        # normalizacja wejścia
        service_id = (service_id or "").strip()

        # 1) Czy calendar_dates zawiera cokolwiek dla tej daty?
        any_for_date = any(
            (row.get("date") or "").strip() == cdate_str
            for row in self.data.get("calendar_dates", [])
        )

        # 2) Jeśli tak, to NIE używamy fallbacku do calendar.txt
        if any_for_date:
            for row in self.data.get("calendar_dates", []):
                if (row.get("service_id") or "").strip() == service_id and (row.get("date") or "").strip() == cdate_str:
                    et = (row.get("exception_type") or "").strip()
                    if et == "1":
                        return True
                    if et == "2":
                        return False
            # brak wpisu dla tego service_id w tym dniu => nieaktywny (bez fallbacku)
            return False

        # 3) Jeśli nie ma żadnych wpisów calendar_dates dla tej daty, stosujemy fallback do calendar.txt
        crows = [c for c in self.data["calendar"] if (c.get("service_id") or "").strip() == service_id]
        if not crows:
            return False

        row = crows[0]  # Przyjmujemy, że dla danego service_id jest tylko jeden wpis

        try:
            start_date = datetime.strptime((row.get("start_date") or "").strip(), "%Y%m%d").date()
            end_date = datetime.strptime((row.get("end_date") or "").strip(), "%Y%m%d").date()
        except Exception as e:
            _LOGGER.error(f"Niepoprawny start_date/end_date w calendar.txt dla service_id={service_id}: {e}")
            return False

        if not (start_date <= check_date <= end_date):
            _LOGGER.debug(f"Check_date poza zakresem – usługa nie kursuje check_date={check_date}")
            return False

        weekday = check_date.weekday()  # 0 = poniedziałek, 6 = niedziela
        mapping = {0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday", 4: "friday", 5: "saturday", 6: "sunday"}
        day_field = mapping.get(weekday)
        if (row.get(day_field) or "").strip() == "1":
            return True
        return False



    def get_next_departures(self, stop_id):
        """
        Zwraca słownik: { linia: [odjazd1, odjazd2], ... }
        uwzględniając weekendy/święta (service_id).
        """
        if "stop_times" not in self.data or "trips" not in self.data:
            _LOGGER.error("❌ Brak danych GTFS!")
            return {}

        now = dt_util.now()  # Upewnienie się, że 'now' jest świadomy strefy czasowej
        today_date = now.date()

        # 1) Zbierz wszystkie kursy z stop_times dla danego stop_id
        departures_all = []
        for row in self.data["stop_times"]:
            if row["stop_id"] != stop_id:
                continue

            trip_id = row["trip_id"]
            trip_info = self.data["trips"].get(trip_id, {})
            line_number = trip_info.get("route_id", "Nieznana")
            trip_headsign = trip_info.get("trip_headsign", "").replace('/', ' ')
            service_id = trip_info.get("service_id", "")

            departure_time = row["departure_time"]
            try:
                dep_time = datetime.strptime(departure_time, "%H:%M:%S").time()
            except ValueError:
                _LOGGER.error(f"Niepoprawny format czasu odjazdu: {departure_time}")
                continue
            dep_datetime_naive = datetime.combine(today_date, dep_time)
            dep_datetime = dt_util.as_local(dep_datetime_naive)  # Zamiana na świadomy strefy czasowej

            departures_all.append({
                "line": line_number,
                "direction": trip_headsign,
                "service_id": service_id,
                "departure_time": departure_time,
                "datetime": dep_datetime
            })

        # Sortowanie po czasie odjazdu
        departures_all.sort(key=lambda x: x["datetime"])

        # Grupa "dziś" w przyszłości, sprawdzając aktywność service_id
        future_today_by_line = {}
        for dep in departures_all:
            if dep["datetime"] >= now:
                # Sprawdzamy, czy service_id jest aktywny
                sid = dep["service_id"]
                if self.service_is_active_on_date(sid, today_date):
                    line = dep["line"]
                    future_today_by_line.setdefault(line, []).append(dep)

        # Jutro
        tomorrow = today_date + timedelta(days=1)
        tomorrow_map = {}

        for dep in departures_all:
            line = dep["line"]
            sid = dep["service_id"]

            newdep = dep.copy()
            newdep["datetime"] = datetime.combine(tomorrow, dep["datetime"].time())
            newdep["datetime"] = dt_util.as_local(newdep["datetime"])  # Zamiana na świadomy strefy czasowej
            newdep["departure_time"] = newdep["datetime"].strftime("%Y-%m-%d %H:%M:%S")

            # Sprawdzamy, czy service_id jest aktywny w jutrzejszym dniu
            if self.service_is_active_on_date(sid, tomorrow):
                tomorrow_map.setdefault(line, []).append(newdep)

        # Sortowanie
        for line in future_today_by_line:
            future_today_by_line[line].sort(key=lambda x: x["datetime"])
        for line in tomorrow_map:
            tomorrow_map[line].sort(key=lambda x: x["datetime"])

        results = {}
        all_lines = set(future_today_by_line.keys()) | set(tomorrow_map.keys())

        for line in all_lines:
            next_deps = []
            # Dzisiaj
            if line in future_today_by_line:
                for dep in future_today_by_line[line]:
                    next_deps.append(dep)
                    if len(next_deps) >= 2:
                        break
            # Jutro
            if len(next_deps) < 2 and line in tomorrow_map:
                for dep in tomorrow_map[line]:
                    next_deps.append(dep)
                    if len(next_deps) >= 2:
                        break

            if next_deps:
                results[line] = next_deps

        _LOGGER.info(f"🚍 Najbliższe kursy (max 2) dla przystanku {stop_id} z weekend/święta: {results}")
        return results

    def get_departures_for_week(self, line, stop_id):
        """
        Zwraca listę wydarzeń odjazdów dla danej linii i przystanku na następne 7 dni.
        """
        events = []
        today = dt_util.now().date()
        now = dt_util.now()
        _LOGGER.debug(f"Generowanie wydarzeń dla linii {line} i przystanku {stop_id} na następne 7 dni.")

        for day_offset in range(1):
            current_day = today + timedelta(days=day_offset)
            for trip_id, trip in self.data["trips"].items():
                if trip.get("route_id") != line:
                    continue
                service_id = trip.get("service_id")
                if not self.service_is_active_on_date(service_id, current_day):
                    continue
                # Znajdź wszystkie stop_times dla tego trip_id i stop_id
                for stop_time in self.data.get("stop_times", []):
                    if stop_time["trip_id"] != trip_id:
                        continue
                    if stop_time["stop_id"] != stop_id:
                        continue

                    departure_time = stop_time["departure_time"]
                    try:
                        dep_time = datetime.strptime(departure_time, "%H:%M:%S").time()
                    except ValueError:
                        _LOGGER.error(f"Niepoprawny format czasu odjazdu: {departure_time}")
                        continue
                    dep_datetime_naive = datetime.combine(current_day, dep_time)
                    dep_datetime = dt_util.as_local(dep_datetime_naive)  # Świadoma strefa czasowa
                    end_datetime = dep_datetime + timedelta(minutes=1)  # Zakładamy, że wydarzenie trwa 1 minutę

                    # Filtruj eventy, które już się zakończyły
                    if end_datetime < now:
                        continue

                    trip_headsign = trip.get("trip_headsign", "Nieznany kierunek").capitalize().replace('/', ' ')

                    event = {
                        "title": f"Linia {line} → {trip_headsign}",
                        "start": dep_datetime.isoformat(),
                        "end": end_datetime.isoformat(),
                        "description": f"Linia {line} → {trip_headsign}",
                        "line": line,
                        "latitude": self.data["stops"][stop_id].get("stop_lat", 0.0),
                        "longitude": self.data["stops"][stop_id].get("stop_lon", 0.0),
                        "stop_name": self.data["stops"][stop_id].get("stop_name", "Nieznany"),
                        "stop_code": self.data["stops"][stop_id].get("stop_code", "")
                    }
                    events.append(event)
                    _LOGGER.debug(f"Dodano wydarzenie: {event}")

        # Sortowanie eventów rosnąco według 'start'
        try:
            events.sort(key=lambda x: datetime.fromisoformat(x["start"]))
            _LOGGER.debug(f"Posortowane wydarzenia: {events}")
        except Exception as e:
            _LOGGER.error(f"❌ Błąd podczas sortowania wydarzeń: {e}")

        _LOGGER.info(f"📅 Zebrano {len(events)} wydarzeń dla linii {line} na przystanku {stop_id}.")
        return events
