import time
import os
from datetime import datetime
import pytz
import tweepy
import exchange_calendars as xcals

from src.core.config import system_config
from src.core.security import check_kill_switch
from src.core.db import get_connection
from src.core.time_utils import NY_TZ, get_ny_time, is_market_open_today, get_current_polling_interval
from src.services.parser import parse_alert
from src.services.notifier import notify_alert, notify_add, notify_review_needed, notify_error, forward_raw_tweet

SINCE_ID_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".since_id")

# Debounce marker for the monthly quota warning (NY date string).
_last_quota_warning_date = None

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
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO api_calls (service, endpoint) VALUES (?, ?)", (service, endpoint))
        conn.commit()
    finally:
        conn.close()

def check_quota_guardrail():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # Check API calls in the current month
        now = datetime.now(NY_TZ)
        start_of_month = f"{now.year}-{now.month:02d}-01 00:00:00"
        
        cursor.execute("SELECT COUNT(*) as count FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,))
        row = cursor.fetchone()
        count = row['count']
    finally:
        conn.close()
    
    limit = system_config.polling.get("monthly_api_call_ceiling", 10000)
    if count >= limit * 0.95:
        # Debounced to once per day: this runs every poll pass, so an undebounced
        # warning would fire ~240x/hour on every channel until month rollover.
        global _last_quota_warning_date
        today_str = datetime.now(NY_TZ).strftime("%Y-%m-%d")
        if _last_quota_warning_date != today_str:
            _last_quota_warning_date = today_str
            notify_error("Poller", f"CRITICAL: Approaching Twitter API monthly quota ({count}/{limit})")

    return count < limit

def start_poller():
    print("Starting Hermes Poller...")
    from src.core.db import log_system_event
    log_system_event('startup', 'Hermes Poller started')
    
    if not system_config.twitter_api_key or not system_config.twitter_access_token:
        print("ERROR: TWITTER_API_KEY or TWITTER_ACCESS_TOKEN not found in .env. Required for private accounts.")
        return
        
    client = tweepy.Client(
        bearer_token=system_config.twitter_bearer_token,
        consumer_key=system_config.twitter_api_key,
        consumer_secret=system_config.twitter_api_secret,
        access_token=system_config.twitter_access_token,
        access_token_secret=system_config.twitter_access_token_secret,
        wait_on_rate_limit=True
    )
    
    # 1. Get Target User ID
    target_account = system_config.polling.get("target_account", "kttechprivate")
    include_retweets = system_config.polling.get("include_retweets", False)
    target_user_id = None
    while not target_user_id:
        try:
            log_api_call('x', 'get_user')
            user = client.get_user(username=target_account)
            target_user_id = user.data.id
            print(f"Target User {target_account} ID: {target_user_id}")
        except Exception as e:
            print(f"Failed to get user ID for {target_account}: {e}. Retrying in 60s...")
            try: 
                notify_error("Poller", f"Failed to get target account ID: {e}")
            except Exception:
                pass
            time.sleep(60)

    while True:
        try:
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
                # Logged before the request: a failed or rate-limited call still
                # consumes quota, so logging only on success undercounts usage
                # and lets the guardrail sail past the real ceiling.
                log_api_call('x', 'get_users_tweets')
                response = client.get_users_tweets(
                    id=target_user_id,
                    since_id=since_id,
                    max_results=50,
                    tweet_fields=["created_at", "referenced_tweets"],
                    user_auth=True
                )
                
                if response.errors:
                    error_msg = str(response.errors)
                    print(f"TWITTER API ERROR: {error_msg}")
                    notify_error("Poller", f"Silent API Error: {error_msg[:100]}...")
                    # Sleep to prevent spamming notifications for the same error
                    time.sleep(300)
                    continue

                
                if response.data:
                    # Tweets are returned newest first. Reverse to process oldest first.
                    tweets = reversed(response.data)
                    highest_id = None
                    
                    for tweet in tweets:
                        print(f"Processing tweet {tweet.id}: {tweet.text[:50]}...")
                        
                        # 0. Safeguard: skip retweets/quote-tweets (spec 2.3). A
                        # retweet of an old #ALERT is timestamped today, so the
                        # created_at check below would not catch it.
                        if not include_retweets:
                            referenced = getattr(tweet, 'referenced_tweets', None) or []
                            ref_types = {getattr(ref, 'type', None) or ref.get('type') for ref in referenced}
                            if ref_types & {'retweeted', 'quoted'}:
                                print(f"Skipping tweet {tweet.id}: retweet/quote-tweet.")
                                highest_id = str(tweet.id)
                                continue

                        # 1. Safeguard: created_at check
                        if hasattr(tweet, 'created_at') and tweet.created_at:
                            tweet_time_ny = tweet.created_at.astimezone(NY_TZ)
                            if tweet_time_ny.date() != get_ny_time().date():
                                print(f"Skipping tweet {tweet.id}: Created at {tweet_time_ny} which is not today.")
                                highest_id = str(tweet.id)
                                continue
                        
                        # Deduplication check
                        conn = get_connection()
                        try:
                            cursor = conn.cursor()
                            cursor.execute("SELECT id FROM alerts WHERE tweet_id = ?", (str(tweet.id),))
                            if cursor.fetchone():
                                # Already processed. Still advance the cursor, or a
                                # batch of known tweets leaves since_id pinned and we
                                # re-fetch the same window every poll, burning quota.
                                highest_id = str(tweet.id)
                                continue
                            
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
                        finally:
                            conn.close()
                        
                        # Notify. 'ignored' tweets are ordinary account activity,
                        # logged for audit but deliberately silent.
                        if signal.parse_status == "ignored":
                            pass
                        elif signal.parse_status == "needs_review":
                            notify_review_needed(tweet.text)
                        elif signal.action == "BTO":
                            notify_alert(signal.ticker, signal.expiry, signal.strike, signal.option_type, signal.price, signal.trade_style, signal.action)
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

        except Exception as e:
            print(f"CRITICAL THREAD ERROR in poller loop: {e}")
            try: 
                notify_error("PollerThread", f"Unexpected crash caught: {e}. Retrying in 60s.")
            except Exception: 
                pass
            time.sleep(60)

if __name__ == "__main__":
    start_poller()
