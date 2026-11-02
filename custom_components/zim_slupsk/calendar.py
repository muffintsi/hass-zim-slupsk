"""Calendar entities with range queries independent of entity state updates."""

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import date, datetime, time, timezone
from time import perf_counter

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def sanitize_name(name):
    """Keep existing entity and unique IDs unchanged."""
    return re.sub(r"[^\w\d_]", "", name.lower().replace(" ", "_"))


def _bound(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(), dt_util.DEFAULT_TIME_ZONE)
    raise ValueError("Expected a date or datetime")


def _calendar_event(event):
    return CalendarEvent(
        summary=event["title"],
        start=event["start"],
        end=event["end"],
        description=event["description"],
    )


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    parser = coordinator.store.parser
    stop_id = coordinator.stop_id
    stop = parser.get_stops()[stop_id]
    name = stop.get("stop_name", "Nieznany")
    code = stop.get("stop_code", "")
    full_name = f"{name} {code.split('/')[-1]}"
    try:
        lat, lon = float(stop.get("stop_lat") or 0), float(stop.get("stop_lon") or 0)
    except (TypeError, ValueError):
        lat = lon = 0.0
    # Discover all lines, even when none operates today or tomorrow.
    lines = await hass.async_add_executor_job(parser.get_lines, stop_id)
    async_add_entities(
        [
            CalendarSensor(
                coordinator,
                stop_id,
                full_name,
                line,
                entry.entry_id,
                f"calendar.linia_{line}_{sanitize_name(full_name)}",
                lat,
                lon,
                name,
                code,
            )
            for line in lines
        ]
    )


class CalendarSensor(CoordinatorEntity, CalendarEntity):
    """A bus line calendar; HA's CalendarEntity supplies the on/off state."""

    _attr_icon = "mdi:calendar"

    def __init__(
        self,
        coordinator,
        stop_id,
        stop_full_name,
        line,
        entry_id,
        entity_id,
        stop_lat,
        stop_lon,
        stop_name,
        stop_code,
    ):
        super().__init__(coordinator)
        self._parser = coordinator.store.parser
        self._stop_id = stop_id
        self._line = line
        self.entity_id = entity_id
        self._attr_name = f"Linia {line} - {stop_full_name}"
        self._attr_unique_id = (
            f"zim_slupsk_calendar_{line}_{sanitize_name(stop_full_name)}_{entry_id}"
        )
        self._attr_extra_state_attributes = {
            "calendar_days": coordinator.calendar_days,
            "line": line,
            "latitude": stop_lat,
            "longitude": stop_lon,
            "stop_name": stop_name,
            "stop_code": stop_code,
        }
        self._event = None
        self._range_cache = OrderedDict()
        self._query_lock = asyncio.Lock()
        self._update_event()

    def _update_event(self):
        event = (self.coordinator.data or {}).get("calendar", {}).get(self._line)
        self._event = _calendar_event(event) if event else None
        self._attr_extra_state_attributes["event_count"] = (
            (self.coordinator.data or {}).get("counts", {}).get(self._line, 0)
        )

    @callback
    def _handle_coordinator_update(self):
        self._update_event()
        super()._handle_coordinator_update()

    @property
    def event(self):
        return self._event

    def _get_events(self, start, end):
        # Counters and query refer to the same immutable feed generation.
        with self._parser._lock:
            hits, misses = self._parser.cache_hits, self._parser.cache_misses
            events = [
                _calendar_event(event)
                for event in self._parser.get_events(self._stop_id, self._line, start, end)
            ]
            return (
                events,
                self._parser.cache_hits - hits,
                self._parser.cache_misses - misses,
                self._parser.generation,
            )

    async def async_get_events(self, hass, start_date, end_date):
        started = perf_counter()
        events, hits, misses = [], 0, 0
        range_hit = False
        try:
            start, end = _bound(start_date), _bound(end_date)
            window_start, window_end = self._parser.calendar_window(self.coordinator.calendar_days)
            start = max(start.astimezone(timezone.utc), window_start.astimezone(timezone.utc))
            end = min(end.astimezone(timezone.utc), window_end.astimezone(timezone.utc))
            if end <= start:
                return []
            async with self._query_lock:
                key = (
                    self._parser.generation,
                    start.astimezone(timezone.utc),
                    end.astimezone(timezone.utc),
                )
                if key in self._range_cache:
                    range_hit = True
                    self._range_cache.move_to_end(key)
                    events = self._range_cache[key]
                else:
                    events, hits, misses, generation = await hass.async_add_executor_job(
                        self._get_events, start, end
                    )
                    # Old snapshots and oversized combined caches must not accumulate.
                    self._range_cache = OrderedDict(
                        (k, v) for k, v in self._range_cache.items() if k[0] == generation
                    )
                    self._range_cache[(generation, key[1], key[2])] = events
                    while (
                        len(self._range_cache) > 4
                        or sum(map(len, self._range_cache.values())) > 5000
                    ):
                        self._range_cache.popitem(last=False)
                return list(events)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            _LOGGER.debug(
                "async_get_events stop=%s line=%s duration=%.4fs events=%d "
                "cache_hits=%d cache_misses=%d range_cache_hit=%s http_requests=0 http_total=%d",
                self._stop_id,
                self._line,
                perf_counter() - started,
                len(events),
                hits,
                misses,
                range_hit,
                self.coordinator.store.downloader.http_requests,
            )
