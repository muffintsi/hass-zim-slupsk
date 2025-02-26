import random
import asyncio
from datetime import datetime, timedelta
import logging

_LOGGER = logging.getLogger(__name__)

def get_random_update_time():
    """
    Generuje losowy czas między 00:00:10 a 03:59:00.
    00:00:10 = 10 sekund po północy,
    03:59:00 = 3*3600 + 59*60 = 10800 + 3540 = 14340 sekund.
    """
    start_seconds = 10
    end_seconds = 14340
    random_seconds = random.randint(start_seconds, end_seconds)
    return (datetime.min + timedelta(seconds=random_seconds)).time()

async def schedule_daily_random_update(hass):
    """
    Uruchamia nieskończoną pętlę, która codziennie o losowym czasie (między 00:00:10 a 03:59:00)
    wywołuje aktualizację danych GTFS (download_gtfs_file(force_update=True)).
    
    W przypadku anulowania taska (np. podczas unload integracji), pętla zostaje przerwana.
    """
    while True:
        now = datetime.now()
        random_time = get_random_update_time()
        target_datetime = datetime.combine(now.date(), random_time)
        if target_datetime <= now:
            target_datetime += timedelta(days=1)
        delay = (target_datetime - now).total_seconds()
        _LOGGER.info(f"Planowana losowa aktualizacja za {delay:.0f} sekund, o {target_datetime}")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            _LOGGER.info("Scheduler został anulowany.")
            break
        try:
            from .gtfs_downloader import download_gtfs_file
            success, url = await download_gtfs_file(force_update=False)
            if success:
                _LOGGER.info("Losowa aktualizacja danych GTFS zakończona sukcesem.")
            else:
                _LOGGER.error("Błąd podczas losowej aktualizacji danych GTFS.")
        except Exception as e:
            _LOGGER.error(f"Błąd w trakcie losowej aktualizacji: {e}")
