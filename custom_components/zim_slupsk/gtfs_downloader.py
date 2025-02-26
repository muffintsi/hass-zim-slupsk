import os
import aiohttp
import aiofiles
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
import json
import zipfile
import csv
from .const import GTFS_URL, GTFS_FILE_PATH

_LOGGER = logging.getLogger(__name__)
METADATA_FILE = os.path.join(os.path.dirname(__file__), "gtfs_metadata.json")

async def async_load_metadata():
    """Asynchronicznie wczytuje metadane (np. ETag) z pliku JSON."""
    if os.path.exists(METADATA_FILE):
        try:
            async with aiofiles.open(METADATA_FILE, "r", encoding="utf-8") as f:
                contents = await f.read()
            return json.loads(contents)
        except Exception as e:
            _LOGGER.error(f"Nie udało się wczytać metadanych: {e}")
    return {}

async def async_save_metadata(metadata):
    """Asynchronicznie zapisuje metadane do pliku JSON."""
    try:
        async with aiofiles.open(METADATA_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(metadata))
    except Exception as e:
        _LOGGER.error(f"Nie udało się zapisać metadanych: {e}")

async def get_gtfs_url():
    """
    Scrapuje stronę i pobiera aktualny URL do pliku GTFS.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://zimslupsk.pl",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1"
     }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GTFS_URL, headers=headers) as response:
                if response.status != 200:
                    _LOGGER.error(f"Nie udało się pobrać strony. Status HTTP: {response.status}")
                    return None

                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                # Szukamy linku z tekstem "tym linkiem"
                link_tag = soup.find("a", string="tym linkiem")
                if link_tag and 'href' in link_tag.attrs:
                    relative_url = link_tag.attrs['href']
                    full_url = urljoin(GTFS_URL, relative_url)
                    _LOGGER.info(f"Znaleziono URL pliku GTFS: {full_url}")
                    return full_url
                else:
                    _LOGGER.error("Nie udało się znaleźć linku do pliku GTFS.")
                    return None
        except Exception as e:
            _LOGGER.error(f"Problem z pobraniem strony: {e}")
            return None

async def get_current_etag(gtfs_url):
    """
    Wykonuje żądanie HEAD do podanego URL-a i zwraca wartość nagłówka ETag.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://zimslupsk.pl/otwarte-dane.html",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Host": "zimslupsk.pl"
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.head(gtfs_url, headers=headers) as response:
                if response.status == 200:
                    etag = response.headers.get("ETag")
                    _LOGGER.debug(f"Uzyskano ETag z serwera: {etag}")
                    return etag
                else:
                    _LOGGER.error(f"Żądanie HEAD nie powiodło się. Status: {response.status}")
                    return None
        except Exception as e:
            _LOGGER.error(f"Błąd podczas żądania HEAD: {e}")
            return None

async def generate_calendar_from_feed_info(gtfs_file_path, output_calendar_path, validity_years=30):
    """
    Asynchronicznie odczytuje feed_info.txt z pliku GTFS (ZIP) i pobiera feed_version,
    a następnie generuje plik calendar.txt asynchronicznie (przy użyciu aiofiles).
    Zakres dat ustawiony jest od dzisiaj do dzisiaj + validity_years lat.
    Wygenerowane service_id: feed_version_NW, feed_version_PF, feed_version_SW.
    """
    feed_version = None
    try:
        with zipfile.ZipFile(gtfs_file_path, 'r') as z:
            if 'feed_info.txt' in z.namelist():
                with z.open('feed_info.txt') as f:
                    lines = f.read().decode('utf-8-sig').splitlines()
                    reader = csv.DictReader(lines)
                    for row in reader:
                        feed_version = row.get("feed_version")
                        break
            else:
                _LOGGER.error("Brak pliku feed_info.txt w GTFS.")
                return False
    except Exception as e:
        _LOGGER.error(f"Błąd podczas odczytu GTFS: {e}")
        return False

    if not feed_version:
        _LOGGER.error("Nie znaleziono feed_version w feed_info.txt.")
        return False

    start_date = datetime.now().strftime("%Y%m%d")
    end_date = (datetime.now() + timedelta(days=365 * validity_years)).strftime("%Y%m%d")

    header = "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    nw_line = f"{feed_version}_NW,0,0,0,0,0,0,1,{start_date},{end_date}\n"
    pf_line = f"{feed_version}_PF,1,1,1,1,1,0,0,{start_date},{end_date}\n"
    sw_line = f"{feed_version}_SW,0,0,0,0,0,1,0,{start_date},{end_date}\n"

    content = header + nw_line + pf_line + sw_line

    try:
        async with aiofiles.open(output_calendar_path, "w", encoding="utf-8") as f:
            await f.write(content)
        _LOGGER.info(f"Calendar.txt wygenerowany pomyślnie: {output_calendar_path}")
        return True
    except Exception as e:
        _LOGGER.error(f"Błąd przy zapisie calendar.txt: {e}")
        return False

async def download_gtfs_file(force_update: bool = False):
    """
    Pobiera plik GTFS z dynamicznego URL i zapisuje go lokalnie.
    Jeśli force_update jest False, sprawdza, czy lokalny plik istnieje.
      - Jeżeli plik istnieje i jest młodszy niż 24 godziny, zwraca (True, None).
      - Jeżeli plik istnieje, ale jest starszy niż 24h, pobiera URL, sprawdza ETag
        i jeżeli ETag się zgadzają, zwraca (True, None); w przeciwnym razie pobiera nową wersję.
    Po pomyślnym pobraniu (lub użyciu lokalnej kopii) sprawdza, czy plik calendar.txt istnieje.
    Jeśli nie – generuje go przy użyciu generate_calendar_from_feed_info().
    Zwraca krotkę: (True, URL) jeśli pobieranie się powiodło, (True, None) gdy używamy lokalnej kopii,
    lub (False, None) w razie błędu.
    """
    # Sprawdzamy lokalny plik GTFS
    if not force_update and os.path.exists(GTFS_FILE_PATH):
        mod_time = os.path.getmtime(GTFS_FILE_PATH)
        mod_datetime = datetime.fromtimestamp(mod_time)
        if datetime.now() - mod_datetime < timedelta(days=1):
            _LOGGER.info("Lokalna kopia pliku GTFS jest młodsza niż 24 godziny – używamy jej.")
            # Sprawdzamy, czy plik calendar.txt istnieje; jeśli nie, generujemy go.
            calendar_txt_path = os.path.join(os.path.dirname(GTFS_FILE_PATH), "calendar.txt")
            if not os.path.exists(calendar_txt_path):
                _LOGGER.warning("Plik calendar.txt nie istnieje – generuję go.")
                await generate_calendar_from_feed_info(GTFS_FILE_PATH, calendar_txt_path)
            return True, None
        else:
            # Plik jest starszy niż 24h – pobieramy URL i sprawdzamy ETag
            gtfs_url = await get_gtfs_url()
            if gtfs_url:
                current_etag = await get_current_etag(gtfs_url)
                metadata = await async_load_metadata()
                saved_etag = metadata.get("etag")
                if current_etag and saved_etag == current_etag:
                    _LOGGER.info("Lokalna kopia pliku GTFS, mimo że starsza niż 24h, jest aktualna (ETag się zgadza).")
                    return True, None
                else:
                    _LOGGER.info("Plik starszy niż 24h i ETag się różni – aktualizujemy plik.")
            else:
                _LOGGER.error("Brak URL do pliku GTFS podczas sprawdzania ETag.")

    # Pobieranie pliku – uzyskujemy URL (jeśli nie został wcześniej pobrany)
    gtfs_url = gtfs_url if 'gtfs_url' in locals() and gtfs_url else await get_gtfs_url()
    if gtfs_url:
        async with aiohttp.ClientSession() as session:
            async with session.get(gtfs_url) as response:
                if response.status == 200:
                    os.makedirs(os.path.dirname(GTFS_FILE_PATH), exist_ok=True)
                    async with aiofiles.open(GTFS_FILE_PATH, "wb") as file:
                        await file.write(await response.read())
                    new_etag = response.headers.get("ETag")
                    metadata = await async_load_metadata()
                    if new_etag:
                        metadata["etag"] = new_etag
                        await async_save_metadata(metadata)
                    _LOGGER.info(f"Pobrano plik GTFS z adresu: {gtfs_url}")
                    # Po pobraniu pliku GTFS generujemy plik calendar.txt
                    output_calendar_path = os.path.join(os.path.dirname(GTFS_FILE_PATH), "calendar.txt")
                    gen_ok = await generate_calendar_from_feed_info(GTFS_FILE_PATH, output_calendar_path)
                    if not gen_ok:
                        _LOGGER.error("Nie udało się wygenerować pliku calendar.txt.")
                    return True, gtfs_url
                else:
                    _LOGGER.error(f"Błąd podczas pobierania pliku GTFS z {gtfs_url}, status: {response.status}")
    else:
        _LOGGER.error("Brak URL do pliku GTFS, pobieranie nie powiodło się.")

    if os.path.exists(GTFS_FILE_PATH):
        _LOGGER.warning("Nie udało się pobrać nowej wersji pliku GTFS, ale lokalny plik istnieje – używamy go.")
        # Sprawdzamy też, czy plik calendar.txt istnieje; jeśli nie – generujemy go.
        calendar_txt_path = os.path.join(os.path.dirname(GTFS_FILE_PATH), "calendar.txt")
        if not os.path.exists(calendar_txt_path):
            _LOGGER.warning("Plik calendar.txt nie istnieje – generuję go.")
            await generate_calendar_from_feed_info(GTFS_FILE_PATH, calendar_txt_path)
        return True, None
    else:
        _LOGGER.error("❌ Brak pliku GTFS – nie można załadować danych!")
        return False, None
