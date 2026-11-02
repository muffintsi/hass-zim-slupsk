DOMAIN = "zim_slupsk"
GTFS_URL = "https://zimslupsk.pl/otwarte-dane.html"  # Stały URL do strony z danymi
GTFS_FILE_PATH = "/config/custom_components/zim_slupsk/gtfs.zip"
MAX_RANGE_DAYS = 62
MAX_EVENTS = 5000
CONF_CALENDAR_DAYS = "calendar_days"
DEFAULT_CALENDAR_DAYS = 7
MAX_CALENDAR_DAYS = 60


def validate_calendar_days(value):
    """Accept whole days only, including integral values from HA's number selector."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Calendar days must be an integer from 1 to 60")
    if not 1 <= value <= MAX_CALENDAR_DAYS or int(value) != value:
        raise ValueError("Calendar days must be an integer from 1 to 60")
    return int(value)
