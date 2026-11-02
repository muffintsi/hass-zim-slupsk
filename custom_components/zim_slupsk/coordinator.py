"""Shared feed loading and one departure coordinator per configuration entry."""

import asyncio
import logging
from datetime import timedelta
from time import monotonic

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_CALENDAR_DAYS, DEFAULT_CALENDAR_DAYS, DOMAIN, validate_calendar_days
from .gtfs_downloader import GTFSDownloader
from .gtfs_parser import GTFSParser

_LOGGER = logging.getLogger(__name__)


class FeedStore:
    def __init__(self, hass):
        self.hass = hass
        path = hass.config.path("custom_components", DOMAIN, "gtfs.zip")
        self.parser = GTFSParser(path)
        self.downloader = GTFSDownloader(hass, path)
        self.lock = asyncio.Lock()
        self.entries = {}

    async def async_refresh(self):
        """Concurrent setup, UI and refreshes share one download and snapshot."""
        async with self.lock:
            if self.parser.data and monotonic() < self.downloader.next_check:
                return
            success, changed = await self.downloader.async_download()
            if not success:
                raise ConfigEntryNotReady("No GTFS data available")
            if changed or not self.parser.data:
                if not await self.parser.load_data(self.hass) and not self.parser.data:
                    raise ConfigEntryNotReady("Invalid GTFS data")


def get_store(hass):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = FeedStore(hass)
    return hass.data[DOMAIN]


def configured_stop(entry):
    return entry.options.get("stop_id", entry.data.get("stop_id"))


class DepartureCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, store):
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=1),
            config_entry=entry,
            always_update=False,
        )
        self.store = store
        self.stop_id = configured_stop(entry)
        try:
            self.calendar_days = validate_calendar_days(
                entry.options.get(CONF_CALENDAR_DAYS, DEFAULT_CALENDAR_DAYS)
            )
        except ValueError:
            self.calendar_days = DEFAULT_CALENDAR_DAYS

    async def _async_update_data(self):
        try:
            await self.store.async_refresh()
            return await self.hass.async_add_executor_job(
                self.store.parser.get_stop_update, self.stop_id, self.calendar_days
            )
        except (ConfigEntryNotReady, ValueError) as err:
            raise UpdateFailed(str(err)) from err
