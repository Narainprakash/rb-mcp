import time
import subprocess
from datetime import datetime, timedelta

from src.core.config import get_user_config, system_config
from src.core.db import get_connection, log_system_event
from src.core.time_utils import NY_TZ, is_market_open_today
from src.services.notifier import NOTIFY_TIMEOUT_SEC

def get_daily_metrics(user_id: int, date_str: str):
    """Fetches PnL, Trades, Alerts, Open Positions, API calls, and Win Rate for the given NY date string."""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Honour date_str so a missed summary can be regenerated for a past day.
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_utc = f"{day} 00:00:00"
        end_utc = f"{day + timedelta(days=1)} 00:00:00"
        
        # Monthly API Calls (Global, not per user)
        now_ny = datetime.now(NY_TZ)
        start_of_month = f"{now_ny.year}-{now_ny.month:02d}-01 00:00:00"
        cursor.execute("SELECT COUNT(*) as monthly_api_calls FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,))
        monthly_api_calls = cursor.fetchone()['monthly_api_calls']
        
        # Trades executed (Buy) for the user
        cursor.execute("SELECT COUNT(*) as trades_placed FROM trades WHERE user_id = ? AND timestamp >= ? AND timestamp < ?", (user_id, start_utc, end_utc))
        trades_placed = cursor.fetchone()['trades_placed']
        
        # Realized PnL from filled Limit Sells for the user
        cursor.execute("SELECT SUM(realized_pnl) as total_pnl FROM limit_orders WHERE user_id = ? AND status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ?", (user_id, start_utc, end_utc))
        total_pnl = cursor.fetchone()['total_pnl'] or 0.0
        
        # Win Rate for the user
        cursor.execute("SELECT COUNT(*) as profitable FROM limit_orders WHERE user_id = ? AND status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ? AND realized_pnl > 0", (user_id, start_utc, end_utc))
        profitable = cursor.fetchone()['profitable']
        
        cursor.execute("SELECT COUNT(*) as total_closed FROM limit_orders WHERE user_id = ? AND status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ?", (user_id, start_utc, end_utc))
        total_closed = cursor.fetchone()['total_closed']
        win_rate = (profitable / total_closed * 100) if total_closed > 0 else 0.0
        
        # Alerts parsed today (Global)
        cursor.execute("SELECT COUNT(*) as alerts_parsed FROM alerts WHERE timestamp >= ? AND timestamp < ?", (start_utc, end_utc))
        alerts_parsed = cursor.fetchone()['alerts_parsed']
        
        # Current open positions for the user
        cursor.execute("SELECT COUNT(*) as open_positions FROM positions WHERE user_id = ? AND status = 'open'", (user_id,))
        open_positions = cursor.fetchone()['open_positions']
    finally:
        conn.close()
    
    return {
        "monthly_api_calls": monthly_api_calls,
        "trades_placed": trades_placed,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "alerts_parsed": alerts_parsed,
        "open_positions": open_positions
    }

def send_summary_notification(user_id: int, summary_text: str):
    config = get_user_config(user_id)
    targets = config.summary.get('targets', ["whatsapp"])
    for target in targets:
        try:
            subprocess.run(["hermes", "send", "--to", target, summary_text], check=True, capture_output=True, timeout=NOTIFY_TIMEOUT_SEC)
            print(f"Daily summary sent to {target} for user {user_id}")
        except Exception as e:
            print(f"Failed to send daily summary to {target} for user {user_id}: {e}")
            log_system_event('error', f"Summary push failed for {target}: {e}", user_id=user_id)

def _last_summary_date(user_id):
    """The NY date this user was last sent a summary, from the database.

    Persisted rather than held in memory: an in-memory flag resets on every
    service restart, so any restart after the send time mails the summary
    again. systemd restarts on failure, so that is not a rare case.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (f"summary_last_sent:{user_id}",)
        ).fetchone()
        return row['value'] if row else None
    finally:
        conn.close()


def _record_summary_sent(user_id, date_str):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, datetime('now', 'localtime'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (f"summary_last_sent:{user_id}", date_str))
        conn.commit()
    finally:
        conn.close()


def summary_loop():
    print("Starting Hermes Daily Summary Scheduler...")
    log_system_event('startup', 'Hermes Daily Summary Scheduler started')
    
    while True:
        try:
            now = datetime.now(NY_TZ)
            current_date_str = now.strftime("%Y-%m-%d")

            # No trading happened, so there is nothing to report. Without this
            # the bot mails a row of zeroes every weekend and holiday.
            if not is_market_open_today():
                time.sleep(300)
                continue

            # Fetch all active users
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE is_active = 1")
                active_users = cursor.fetchall()
            finally:
                conn.close()
            
            for user_row in active_users:
                user_id = user_row['id']
                config = get_user_config(user_id)
                
                if not config.summary.get('enabled', True):
                    continue
                    
                target_time_str = config.summary.get('time', '16:30')
                target_time = datetime.strptime(target_time_str, "%H:%M").time()
                
                # Check if it's past the target time and we haven't sent it today
                if now.time() >= target_time and _last_summary_date(user_id) != current_date_str:
                    metrics = get_daily_metrics(user_id, current_date_str)
                    
                    # Format message
                    sign = "+" if metrics['total_pnl'] >= 0 else ""
                    paper_mode = config.execution.get("paper_mode", True)
                    mode_str = "PAPER" if paper_mode else "LIVE"
                    api_limit = system_config.polling.get("monthly_api_call_ceiling", 10000)
                    
                    summary_text = (
                        f"📊 *Hermes Daily Summary ({mode_str})*\n"
                        f"📅 {current_date_str}\n\n"
                        f"• Realized P/L: {sign}${metrics['total_pnl']:.2f}\n"
                        f"• Win Rate: {metrics['win_rate']:.0f}%\n"
                        f"• Trades Executed: {metrics['trades_placed']}\n"
                        f"• Alerts Parsed: {metrics['alerts_parsed']}\n"
                        f"• Open Positions: {metrics['open_positions']}\n"
                        f"• X API Quota: {metrics['monthly_api_calls']} / {api_limit}"
                    )
                    
                    send_summary_notification(user_id, summary_text)
                    log_system_event('system', f"Daily summary generated and sent for {current_date_str}", user_id=user_id)
                    _record_summary_sent(user_id, current_date_str)


        except Exception as e:
            print(f"Error in summary loop: {e}")
            log_system_event('error', f"Summary loop error: {e}")
            
        # Check every 60 seconds
        time.sleep(60)

if __name__ == "__main__":
    summary_loop()

