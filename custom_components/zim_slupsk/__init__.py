"""ZIM Słupsk integration lifecycle."""

from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import DepartureCoordinator, configured_stop, get_store

PLATFORMS = (Platform.SENSOR, Platform.CALENDAR)


async def async_setup(hass, config):
    get_store(hass)
    return True


async def _async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass, entry):
    store = get_store(hass)
    await store.async_refresh()
    if configured_stop(entry) not in store.parser.get_stops():
        raise ConfigEntryNotReady("Configured stop is missing from GTFS")
    coordinator = DepartureCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    store.entries[entry.entry_id] = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_shutdown()
        store.entries.pop(entry.entry_id, None)
        raise
    # Current HA owns automatic options reloads; old releases need a listener.
    if not hasattr(config_entries, "OptionsFlowWithReload"):
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
    coordinator.store.entries.pop(entry.entry_id, None)
    entry.runtime_data = None
    # Retain one bounded feed/cache for UI and reload; no task or timer survives.
    return True
