from datetime import datetime, time, timedelta
import pytz
import exchange_calendars as xcals
from src.core.config import system_config

NY_TZ = pytz.timezone(system_config.polling.get("timezone", "America/New_York"))

def get_ny_time():
    return datetime.now(NY_TZ)

def is_market_open_today():
    if not system_config.polling.get("skip_market_holidays", True):
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
    
    windows = system_config.polling.get("windows", [])
    for window in windows:
        start_time = datetime.strptime(window["start"], "%H:%M").time()
        end_time = datetime.strptime(window["end"], "%H:%M").time()
        
        if start_time <= now <= end_time:
            return window["interval_sec"]
            
    return None

def get_today_ny_bounds():
    """Returns the local NY string start and end of the current NY calendar day."""
    now_ny = datetime.now(NY_TZ)
    # Start of today in NY
    start_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    # Start of tomorrow in NY
    end_ny = start_ny + timedelta(days=1)
    
    # Format directly as string since SQLite now uses localtime
    start_str = start_ny.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_ny.strftime("%Y-%m-%d %H:%M:%S")
    return start_str, end_str
