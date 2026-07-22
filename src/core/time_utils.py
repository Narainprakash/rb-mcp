from datetime import datetime, time, timedelta
import pytz
import exchange_calendars as xcals
from src.core.config import config

NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

def get_ny_time():
    return datetime.now(NY_TZ)

def is_market_open_today():
    if not config.polling.get("skip_market_holidays", True):
        return True
    
    try:
        nyse = xcals.get_calendar("NYSE")
        today_str = datetime.now(NY_TZ).strftime("%Y-%m-%d")
        return nyse.is_session(today_str)
    except Exception as e:
        print(f"WARNING: Calendar check failed ({e}). Defaulting to open market.")
        return True

def get_current_polling_interval():
    now = datetime.now(NY_TZ).time()
    
    windows = config.polling.get("windows", [])
    for window in windows:
        start_time = datetime.strptime(window["start"], "%H:%M").time()
        end_time = datetime.strptime(window["end"], "%H:%M").time()
        
        if start_time <= now <= end_time:
            return window["interval_sec"]
            
    return None

def get_today_utc_bounds():
    """Returns the UTC string start and end of the current NY calendar day."""
    now_ny = datetime.now(NY_TZ)
    # Start of today in NY
    start_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    # Start of tomorrow in NY
    end_ny = start_ny + timedelta(days=1)
    
    # Convert to UTC string format matching SQLite CURRENT_TIMESTAMP
    start_utc = start_ny.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_ny.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc
