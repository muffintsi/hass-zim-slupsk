import logging
import voluptuous as vol

from homeassistant import config_entries
from .gtfs_parser import GTFSParser
from .gtfs_downloader import download_gtfs_file  # Importujemy nowy moduł
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class ZimSlupskConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow dla ZIM Słupsk."""

    async def async_step_user(self, user_input=None):
        """Pierwszy krok - pobranie GTFS i wybór przystanku."""
        errors = {}
        stops_data = {}  # **🔹 Zapewniamy, że stops_data ZAWSZE istnieje**

        parser = GTFSParser()

        # Pobranie danych GTFS
        #success, gtfs_url = await parser.download_gtfs()
        success, gtfs_url = await download_gtfs_file(force_update=True)
        if not success:
            errors["base"] = "error_downloading_gtfs"
            _LOGGER.error("❌ Błąd pobierania GTFS.")
            return self.async_abort(reason="error_downloading_gtfs")

        # Wczytanie danych – wywołanie asynchroniczne!
        success = await parser.load_data()
        if not success:
            errors["base"] = "error_loading_gtfs"
            _LOGGER.error("❌ Błąd ładowania GTFS.")
            return self.async_abort(reason="error_loading_gtfs")

        # Pobranie listy przystanków
        stops_data = parser.get_stops()
        _LOGGER.debug(f"🔍 Stops_data przekazane do config_flow: {stops_data}")

        if not stops_data:
            errors["base"] = "stops_data_unavailable"
            _LOGGER.error("❌ stops_data jest puste!")
            return self.async_abort(reason="stops_data_unavailable")

        # Formatowanie przystanków: {stop_name} {fragment za znakiem / w stop_code}
        stops = {
            stop_id: f"{stop_data.get('stop_name', 'Nieznany')} {stop_data.get('stop_code', '').split('/')[-1]}"
            for stop_id, stop_data in stops_data.items()
        }

        # Sortowanie przystanków alfabetycznie
        sorted_stops = dict(sorted(stops.items(), key=lambda item: item[1]))

        # Jeśli to pierwsze wywołanie, pokazujemy formularz z wyborem przystanku
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required("stop_id"): vol.In(sorted_stops)}),
                errors=errors
            )

        # **🔹 Poprawne pobranie stop_name i stop_code_fragment**
        stop_id = user_input["stop_id"]
        stop_name = stops_data.get(stop_id, {}).get("stop_name", "Nieznany")
        stop_code = stops_data.get(stop_id, {}).get("stop_code", "")

        _LOGGER.info(f"📝 Utworzono integrację dla: {stop_name}, stop_code: {stop_code}")

        # Pobranie fragmentu za "/"
        stop_code_fragment = stop_code.split("/")[-1] if "/" in stop_code else stop_code

        return self.async_create_entry(title=f"ZIM Słupsk - {stop_name} {stop_code_fragment}", data=user_input)
