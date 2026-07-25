import threading
import time
from datetime import datetime
from src.core.config import get_user_config
from src.core.db import init_db, get_connection
from src.services.poller import start_poller
from src.services.decision import compute_decision, log_decision
from src.services.executor import execute_trade, get_live_quote
from src.services.quotes import QuoteUnavailable
from src.services.monitor import process_open_orders
from src.services.notifier import notify_skipped
from src.services.summary import summary_loop
from src.core.security import check_kill_switch, check_secret_file_permissions, trading_halted
from src.core.time_utils import NY_TZ, get_current_polling_interval, get_today_ny_bounds


def signal_age_seconds(alert):
    """Seconds between the tweet being posted and now, in market time.

    Falls back to when we recorded the alert if the tweet timestamp is missing,
    which is the conservative choice: it can only under-report age slightly, it
    cannot make an old signal look fresh.
    """
    stamp = alert['tweet_created_at'] if 'tweet_created_at' in alert.keys() and alert['tweet_created_at'] else alert['timestamp']
    if not stamp:
        return None
    try:
        recorded = NY_TZ.localize(datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None
    return (datetime.now(NY_TZ) - recorded).total_seconds()

def trade_loop():
    """
    Independent loop that reads unprocessed alerts from the database,
    makes trading decisions, executes them, and monitors open orders.
    Fans out to all active users.
    """
    print("Starting Hermes Trade & Execution Loop...")
    from src.core.db import log_system_event
    from src.services.notifier import notify_error, notify_skipped
    log_system_event('startup', 'Hermes Trade & Execution Loop started')

    from src.core.config import system_config
    monitor_interval = system_config.settings.get('execution', {}).get('monitor_interval_sec', 15)
    last_monitor_run = 0.0

    while True:
        try:
            # 1. Check Kill Switch
            check_kill_switch()
            
            # 2. Check if we should be active
            interval = get_current_polling_interval()
            if interval is None:
                # Outside market/polling hours, sleep long to save API calls
                time.sleep(60)
                continue
                
            # 3. Process Open Orders (Take-Profit & 0DTE cutoffs) - Handles all users internally.
            # Throttled: this quotes every open order for every user, so running it
            # on the raw 2s loop would scale quote volume with users x positions.
            if time.monotonic() - last_monitor_run >= monitor_interval:
                process_open_orders()
                last_monitor_run = time.monotonic()
            
            # Fetch active users
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE is_active = 1")
                active_users = [row['id'] for row in cursor.fetchall()]
            finally:
                conn.close()

            # Granular halt: keep polling and managing exits, open nothing new.
            if trading_halted():
                time.sleep(2)
                continue

            for user_id in active_users:
                # 4. Find alerts that haven't been decisioned yet for THIS user
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    # Bound to the current NY trading day. Without this, a newly added
                    # or reactivated user would have every historical alert returned
                    # here and executed at once against stale signals.
                    start_ny, end_ny = get_today_ny_bounds()
                    cursor.execute("""
                        SELECT a.* FROM alerts a
                        LEFT JOIN decisions d ON a.id = d.alert_id AND d.user_id = ?
                        WHERE a.parse_status = 'success' AND d.id IS NULL
                          AND a.timestamp >= ? AND a.timestamp < ?
                        ORDER BY a.timestamp ASC
                    """, (user_id, start_ny, end_ny))
                    pending_alerts = cursor.fetchall()
                finally:
                    conn.close()
                
                for alert in pending_alerts:
                    print(f"Processing decision for User {user_id}, Alert ID {alert['id']} (${alert['ticker']} {alert['action']})")

                    # 4a. Staleness gate. The day bound above stops historical
                    # backfill, but after downtime (systemd restarts us on crash)
                    # the gap's alerts are still undecided and would otherwise be
                    # executed at market minutes or hours late.
                    age_sec = signal_age_seconds(alert)
                    max_age = get_user_config(user_id).decision.get('max_signal_age_sec', 120)
                    if max_age and age_sec is not None and age_sec > max_age:
                        reasoning = f"signal is {age_sec:.0f}s old, exceeding max_signal_age_sec {max_age}"
                        log_decision(user_id, alert['id'], alert['price'], None, "skip", reasoning)
                        notify_skipped(user_id, reasoning)
                        continue

                    # Fetch live quote for the decision engine
                    try:
                        quote = get_live_quote(alert['ticker'], alert['expiry'], alert['strike'], alert['option_type'], alert['price'], user_id=user_id)
                    except QuoteUnavailable as e:
                        # Record the skip so the alert isn't retried every 2s.
                        reasoning = f"no quote available: {e}"
                        log_decision(user_id, alert['id'], alert['price'], None, "skip", reasoning)
                        notify_skipped(user_id, reasoning)
                        continue
                    live_ask = quote['ask']

                    # 5. Compute decision based on price tolerance and risk limits
                    action, reasoning, contracts = compute_decision(
                        user_id,
                        alert['id'],
                        alert['ticker'],
                        alert['expiry'],
                        alert['strike'],
                        alert['option_type'],
                        alert['price'],
                        live_ask,
                        alert['trade_style']
                    )

                    # Log decision, including how long the signal took to reach us.
                    decision_id = log_decision(user_id, alert['id'], alert['price'], live_ask, action, reasoning, latency_sec=age_sec)

                    # Notify on every skip, not just price-tolerance ones: a
                    # silently skipped alert looks identical to no alert at all.
                    if action == "skip":
                        notify_skipped(user_id, reasoning)
                    
                    # 6. Execute if within tolerance and limits
                    if action in ("market_buy", "limit_buy"):
                        execute_trade(
                            user_id=user_id,
                            decision_id=decision_id,
                            alert_id=alert['id'],
                            ticker=alert['ticker'], 
                            expiry=alert['expiry'], 
                            strike=alert['strike'], 
                            option_type=alert['option_type'],
                            signal_action=alert['action'],
                            decision_action=action,
                            recommended_price=alert['price'],
                            contracts=contracts
                        )
            
        except SystemExit:
            raise  # Allow kill switch sys.exit(1) to propagate
        except Exception as e:
            print(f"Error in trade loop: {e}")
            try:
                notify_error("TradeLoop", str(e))
            except Exception:
                pass  # Don't let notification failures crash the loop
            
        # Run trade loop frequently to catch limit sell fills quickly
        time.sleep(2)

if __name__ == "__main__":
    print("Initializing Database...")
    init_db()
    check_secret_file_permissions()
    
    print("Starting Background Threads...")
    # Start poller thread
    poller_thread = threading.Thread(target=start_poller, daemon=True)
    poller_thread.start()
    
    # Start summary thread
    summary_thread = threading.Thread(target=summary_loop, daemon=True)
    summary_thread.start()
    
    # Start trade loop in main thread
    trade_loop()
