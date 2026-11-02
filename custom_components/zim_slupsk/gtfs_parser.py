"""Indexed, thread-safe GTFS snapshots; queries belong in an executor."""

import asyncio
import csv
import io
import logging
import threading
import zipfile
from collections import OrderedDict, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .const import GTFS_FILE_PATH, MAX_EVENTS, MAX_RANGE_DAYS

_LOGGER = logging.getLogger(__name__)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MAX_CACHED_DAYS = 128
MAX_CACHED_EVENTS = 50000


async def async_executor_job(hass, target, *args):
    """Keep a file transaction serialized until its worker finishes on cancellation."""
    future = asyncio.ensure_future(hass.async_add_executor_job(target, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.shield(future)
        raise


def read_feed(path):
    """Validate a complete archive without publishing partial state."""
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError("GTFS archive exceeds 256 MiB uncompressed")
        members = {Path(name).name: name for name in archive.namelist()}

        def rows(name, required=False):
            if name not in members:
                if required:
                    raise ValueError(f"Missing {name}")
                return []
            with archive.open(members[name]) as stream:
                return [
                    {key.strip(): (value or "").strip() for key, value in row.items() if key}
                    for row in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
                ]

        data = {
            name: rows(name + ".txt", name in ("stops", "trips", "stop_times"))
            for name in ("stops", "trips", "stop_times", "calendar", "calendar_dates", "agency")
        }
        data["stops"] = {row["stop_id"]: row for row in data["stops"]}
        data["trips"] = {row["trip_id"]: row for row in data["trips"]}
        if not data["stops"] or not data["trips"]:
            raise ValueError("Empty GTFS stops or trips")
        if not data["calendar"] and not data["calendar_dates"]:
            raise ValueError("GTFS has no service calendar")
        return data


class GTFSParser:
    """One snapshot with bounded LRU cache shared by all entities."""

    def __init__(self, path=GTFS_FILE_PATH):
        self.path = path
        self.data = {}
        self.stop_id_map = {}
        self.generation = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._cached_events = 0
        self._by_stop = {}
        self._calendar = {}
        self._exceptions = {}
        self._fallback_services = set()
        self._exception_dates = set()
        self._max_day_offset = 0
        self.tz = ZoneInfo("Europe/Warsaw")

    async def load_data(self, hass=None):
        """ZIP, CSV and indexing must never run on the HA event loop."""
        if hass is not None:
            return await async_executor_job(hass, self._load_data)
        return await asyncio.to_thread(self._load_data)

    def _load_data(self):
        try:
            data = read_feed(self.path)
            by_stop = defaultdict(list)
            max_offset = 0
            for row in data["stop_times"]:
                trip = data["trips"].get(row["trip_id"])
                if not trip:
                    continue
                try:
                    hour, minute, second = map(int, row["departure_time"].split(":"))
                    if not (0 <= hour <= 167 and 0 <= minute < 60 and 0 <= second < 60):
                        raise ValueError("Invalid GTFS time")
                except (KeyError, ValueError):
                    _LOGGER.warning(
                        "Ignoring invalid departure time: %s", row.get("departure_time")
                    )
                    continue
                seconds = hour * 3600 + minute * 60 + second
                max_offset = max(max_offset, seconds // 86400)
                by_stop[row["stop_id"]].append((seconds, row["trip_id"], trip))
            calendar = {}
            fallback_services = set()
            for row in data["calendar"]:
                days = tuple(row.get(day) == "1" for day in _DAYS)
                end = datetime.strptime(row["end_date"], "%Y%m%d").date()
                # ZIM publishes an enumerated calendar with zero weekday flags.
                # Preserve the integration's PS/SO/NO fallback beyond enumerated days,
                # but never apply it to holidays with an explicit service selection.
                suffix = row["service_id"].rsplit("_", 1)[-1]
                if not any(days) and suffix in ("PS", "SO", "NO"):
                    days = tuple(
                        i < 5 if suffix == "PS" else i == (5 if suffix == "SO" else 6)
                        for i in range(7)
                    )
                    end = max(end, dt_util.utcnow().date() + timedelta(days=365 * 30))
                    fallback_services.add(row["service_id"])
                calendar[row["service_id"]] = (
                    datetime.strptime(row["start_date"], "%Y%m%d").date(),
                    end,
                    days,
                )
            exceptions = {
                (row["service_id"], datetime.strptime(row["date"], "%Y%m%d").date()): row[
                    "exception_type"
                ]
                == "1"
                for row in data["calendar_dates"]
            }
            tz = ZoneInfo(
                next(
                    (
                        row["agency_timezone"]
                        for row in data["agency"]
                        if row.get("agency_timezone")
                    ),
                    "Europe/Warsaw",
                )
            )
            with self._lock:
                self.data = data
                self._by_stop = dict(by_stop)
                self._calendar = calendar
                self._exceptions = exceptions
                self._fallback_services = fallback_services
                self._exception_dates = {day for _, day in exceptions}
                self._max_day_offset = max_offset
                self.tz = tz
                self.stop_id_map = {
                    f"{s.get('stop_name', '')} {s.get('stop_code', '')}".strip(): sid
                    for sid, s in data["stops"].items()
                }
                self._cache.clear()
                self._cached_events = 0
                self.generation += 1
            return True
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as err:
            _LOGGER.error("Cannot load GTFS; keeping previous snapshot: %s", err)
            return False

    def get_stops(self):
        return self.data.get("stops", {})

    def get_lines(self, stop_id):
        with self._lock:
            return sorted({trip["route_id"] for _, _, trip in self._by_stop.get(stop_id, ())})

    def service_is_active_on_date(self, service_id, check_date: date):
        service_id = (service_id or "").strip()
        exception = self._exceptions.get((service_id, check_date))
        if exception is not None:
            return exception
        if service_id in self._fallback_services and check_date in self._exception_dates:
            return False
        rule = self._calendar.get(service_id)
        return bool(rule and rule[0] <= check_date <= rule[1] and rule[2][check_date.weekday()])

    def _day(self, stop_id, service_date):
        key = (stop_id, service_date)
        if key in self._cache:
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]
        self.cache_misses += 1
        active = {}
        events = []
        seen = set()
        for seconds, trip_id, trip in self._by_stop.get(stop_id, ()):
            sid = trip.get("service_id", "")
            if sid not in active:
                active[sid] = self.service_is_active_on_date(sid, service_date)
            if not active[sid]:
                continue
            identity = (trip_id, seconds)
            if identity in seen:
                continue
            seen.add(identity)
            # GTFS defines a service day as noon minus 12 hours, including DST days.
            noon = datetime.combine(service_date, time(12), self.tz).astimezone(timezone.utc)
            start = (noon + timedelta(seconds=seconds - 43200)).astimezone(self.tz)
            end = (start.astimezone(timezone.utc) + timedelta(minutes=1)).astimezone(self.tz)
            direction = trip.get("trip_headsign", "Nieznany kierunek").replace("/", " ")
            line = trip.get("route_id", "Nieznana")
            title = f"Linia {line} → {direction.capitalize()}"
            events.append(
                {
                    "title": title,
                    "description": title,
                    "start": start,
                    "end": end,
                    "line": line,
                    "direction": direction,
                    "service_id": sid,
                }
            )
            if len(events) > MAX_CACHED_EVENTS:
                raise ValueError("Too many departures for one stop/day")
        events.sort(key=lambda event: event["start"].timestamp())
        result = tuple(events)
        self._cache[key] = result
        self._cached_events += len(result)
        while len(self._cache) > MAX_CACHED_DAYS or self._cached_events > MAX_CACHED_EVENTS:
            _, old = self._cache.popitem(last=False)
            self._cached_events -= len(old)
        return result

    def get_events(self, stop_id, line, start, end, max_events=MAX_EVENTS):
        """Exclusive overlap bounds, including services departing after 24:00."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Calendar bounds must include a timezone")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            return []
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            raise ValueError(f"Calendar range cannot exceed {MAX_RANGE_DAYS} days")
        with self._lock:
            first = start.astimezone(self.tz).date() - timedelta(days=self._max_day_offset + 1)
            last = end.astimezone(self.tz).date()
            events = []
            for offset in range((last - first).days + 1):
                for event in self._day(stop_id, first + timedelta(days=offset)):
                    if line is not None and event["line"] != line:
                        continue
                    if (
                        event["end"].astimezone(timezone.utc) > start
                        and event["start"].astimezone(timezone.utc) < end
                    ):
                        events.append(event)
                        if max_events is not None and len(events) > max_events:
                            raise ValueError(
                                f"Calendar result exceeds {MAX_EVENTS} events; request a smaller range"
                            )
            return sorted(events, key=lambda event: event["start"].timestamp())

    def get_next_departures(self, stop_id):
        return self.get_stop_update(stop_id)["departures"]

    def calendar_window(self, days):
        """N calendar days starting today, in the agency timezone."""
        today = dt_util.utcnow().astimezone(self.tz).date()
        return (
            datetime.combine(today, time(), self.tz),
            datetime.combine(today + timedelta(days=days), time(), self.tz),
        )

    def get_stop_update(self, stop_id, calendar_days=7):
        """Batch the next sensor departures and calendar event for every line."""
        now = dt_util.utcnow()
        with self._lock:
            results = {}
            calendar = {}
            counts = defaultdict(int)
            _, calendar_end = self.calendar_window(calendar_days)
            sensor_end = now + timedelta(days=7)
            end = max(sensor_end, calendar_end.astimezone(timezone.utc))
            for event in self.get_events(stop_id, None, now, end, max_events=None):
                start = event["start"].astimezone(timezone.utc)
                if start < calendar_end.astimezone(timezone.utc):
                    calendar.setdefault(event["line"], event)
                    counts[event["line"]] += 1
                if start < now or start >= sensor_end:
                    continue
                departures = results.setdefault(event["line"], [])
                if len(departures) < 2:
                    departures.append(
                        {
                            **event,
                            "datetime": event["start"],
                            "departure_time": event["start"].isoformat(),
                        }
                    )
            return {
                "departures": results,
                "calendar": calendar,
                "counts": dict(counts),
                "generation": self.generation,
            }

    def get_departures_for_week(self, line, stop_id):
        now = dt_util.utcnow()
        return self.get_events(stop_id, line, now, now + timedelta(days=7))
