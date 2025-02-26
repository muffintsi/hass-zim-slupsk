import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from .gtfs_parser import GTFSParser
from .gtfs_downloader import download_gtfs_file
from .const import DOMAIN
from .scheduler import schedule_daily_random_update

_LOGGER = logging.getLogger(__name__)
# DOMAIN jest importowany z const.py

async def async_setup(hass: HomeAssistant, config: dict):
    """Inicjalizuje integrację zim_slupsk."""
    _LOGGER.info("✅ Inicjalizowanie integracji zim_slupsk")
    gtfs_parser = GTFSParser()
    hass.data[DOMAIN] = gtfs_parser
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Inicjalizowanie wpisu konfiguracji integracji zim_slupsk."""
    _LOGGER.info(f"✅ Inicjalizowanie wpisu konfiguracji dla {entry.title}")

    parser = hass.data[DOMAIN]
    success, gtfs_url = await download_gtfs_file()

    if success:
        _LOGGER.info(f"📥 Plik GTFS został pobrany pomyślnie: {gtfs_url if gtfs_url else 'Używany lokalny plik'}")
        if await parser.load_data():
            _LOGGER.info("📂 Dane z plików GTFS zostały wczytane.")
        else:
            _LOGGER.error("❌ Błąd wczytywania danych z plików GTFS.")
            raise ConfigEntryNotReady
    else:
        _LOGGER.error("❌ Błąd pobierania pliku GTFS.")
        raise ConfigEntryNotReady

    hass.data[DOMAIN] = parser

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "calendar"])

    # Ustawiamy scheduler dla tego wpisu na podstawie entry.entry_id.
    scheduler_key = f"{DOMAIN}_scheduler_{entry.entry_id}"
    # Jeśli dla tego entry istnieje już scheduler, anulujemy go.
    if scheduler_task := hass.data.get(scheduler_key):
        scheduler_task.cancel()
    # Uruchamiamy scheduler dopiero po zdarzeniu "homeassistant_started"
    async def start_scheduler(event):
        task = hass.async_create_task(schedule_daily_random_update(hass))
        hass.data[scheduler_key] = task
        _LOGGER.info(f"Scheduler uruchomiony dla entry {entry.entry_id}")
    hass.bus.async_listen_once("homeassistant_started", start_scheduler)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Usuwa integrację."""
    _LOGGER.info(f"🛑 Usuwanie integracji {entry.title}")
    unload_ok_sensor = await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    unload_ok_calendar = await hass.config_entries.async_forward_entry_unload(entry, "calendar")
    scheduler_key = f"{DOMAIN}_scheduler_{entry.entry_id}"
    scheduler_task = hass.data.get(scheduler_key)
    if scheduler_task:
        scheduler_task.cancel()
        _LOGGER.info(f"Scheduler anulowany dla entry {entry.entry_id}")
    if unload_ok_sensor and unload_ok_calendar:
        return True
    return False
