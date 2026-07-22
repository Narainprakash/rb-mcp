import time
import os
from datetime import datetime
import pytz
import tweepy
import exchange_calendars as xcals

from src.core.config import config
from src.core.security import check_kill_switch
from src.core.db import get_connection
from src.services.parser import parse_alert
from src.services.notifier import notify_alert, notify_add, notify_review_needed, notify_error, forward_raw_tweet

SINCE_ID_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".since_id")
NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

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
    """
    Returns the polling interval in seconds based on the current ET time,
    or None if outside polling windows.
    """
    now = datetime.now(NY_TZ).time()
    
    windows = config.polling.get("windows", [])
    for window in windows:
        start_time = datetime.strptime(window["start"], "%H:%M").time()
        end_time = datetime.strptime(window["end"], "%H:%M").time()
        
        if start_time <= now <= end_time:
            return window["interval_sec"]
            
    return None # Outside all windows

def get_last_since_id():
    if os.path.exists(SINCE_ID_FILE):
        with open(SINCE_ID_FILE, "r") as f:
            content = f.read().strip()
            return content if content else None
    return None

def set_last_since_id(since_id):
    with open(SINCE_ID_FILE, "w") as f:
        f.write(str(since_id))

def log_api_call(service, endpoint):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO api_calls (service, endpoint) VALUES (?, ?)", (service, endpoint))
    conn.commit()
    conn.close()

def check_quota_guardrail():
    conn = get_connection()
    cursor = conn.cursor()
    # Check API calls in the current month
    now = datetime.now(NY_TZ)
    start_of_month = f"{now.year}-{now.month:02d}-01 00:00:00"
    
    cursor.execute("SELECT COUNT(*) as count FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,))
    row = cursor.fetchone()
    count = row['count']
    conn.close()
    
    limit = config.polling.get("monthly_api_call_ceiling", 10000)
    if count >= limit * 0.95:
        notify_error("Poller", f"CRITICAL: Approaching Twitter API monthly quota ({count}/{limit})")
    
    return count < limit

def start_poller():
    print("Starting Hermes Poller...")
    from src.core.db import log_system_event
    log_system_event('startup', 'Hermes Poller started')
    
    if not config.twitter_bearer_token:
        print("ERROR: TWITTER_BEARER_TOKEN not found in .env")
        return
        
    client = tweepy.Client(bearer_token=config.twitter_bearer_token)
    
    # 1. Get Target User ID
    target_account = config.polling.get("target_account", "kttechprivate")
    try:
        user = client.get_user(username=target_account)
        target_user_id = user.data.id
        log_api_call('x', 'get_user')
        print(f"Target User {target_account} ID: {target_user_id}")
    except Exception as e:
        print(f"Failed to get user ID for {target_account}: {e}")
        notify_error("Poller", f"Failed to get target account ID: {e}")
        return

    while True:
        # Check Kill Switch at the start of every loop
        check_kill_switch()
        
        if not is_market_open_today():
            print("Market is closed today. Sleeping for 1 hour...")
            time.sleep(3600)
            continue
            
        interval = get_current_polling_interval()
        if interval is None:
            # Sleep until the next minute and check again
            time.sleep(60)
            continue
            
        if not check_quota_guardrail():
            print("Monthly API quota exceeded. Poller sleeping.")
            time.sleep(3600)
            continue

        since_id = get_last_since_id()
        
        try:
            # Poll Twitter
            response = client.get_users_tweets(
                id=target_user_id,
                since_id=since_id,
                max_results=5,
                tweet_fields=["created_at"]
            )
            log_api_call('x', 'get_users_tweets')
            
            if response.data:
                # Tweets are returned newest first. Reverse to process oldest first.
                tweets = reversed(response.data)
                highest_id = None
                
                for tweet in tweets:
                    print(f"Processing tweet {tweet.id}: {tweet.text[:50]}...")
                    
                    # Deduplication check
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT id FROM alerts WHERE tweet_id = ?", (str(tweet.id),))
                    if cursor.fetchone():
                        conn.close()
                        continue # Already processed
                    
                    # Parse Alert
                    signal = parse_alert(tweet.text)
                    
                    # Save Alert to DB
                    cursor.execute("""
                        INSERT INTO alerts (tweet_id, raw_text, action, ticker, expiry, strike, option_type, price, trade_style, parse_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (str(tweet.id), tweet.text, signal.action, signal.ticker, signal.expiry, signal.strike, 
                          signal.option_type, signal.price, signal.trade_style, signal.parse_status))
                    alert_id = cursor.lastrowid
                    conn.commit()
                    conn.close()
                    
                    # Notify
                    if signal.parse_status == "needs_review":
                        notify_review_needed(tweet.text)
                    elif signal.action == "BTO":
                        notify_alert(signal.action, signal.ticker, signal.expiry, signal.strike, signal.option_type, signal.price, signal.trade_style)
                        forward_raw_tweet(tweet.text)
                    elif signal.action == "ADD":
                        notify_add(signal.ticker, signal.expiry, signal.strike, signal.option_type, signal.price)
                        forward_raw_tweet(tweet.text)
                    
                    highest_id = str(tweet.id)
                
                if highest_id:
                    set_last_since_id(highest_id)
                    
        except Exception as e:
            print(f"Error fetching tweets: {e}")
            notify_error("Poller", str(e))
            
        # Sleep for the configured cadence
        time.sleep(interval)

if __name__ == "__main__":
    start_poller()
