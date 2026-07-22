import time
import subprocess
from datetime import datetime
import pytz

from src.core.config import config
from src.core.db import get_connection, log_system_event

NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

def get_daily_metrics(date_str):
    """Fetches PnL, Trades, and API calls for the given date (YYYY-MM-DD)"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # API Calls
    cursor.execute("SELECT COUNT(*) as api_calls FROM api_calls WHERE date(timestamp) = ?", (date_str,))
    api_calls = cursor.fetchone()['api_calls']
    
    # Trades executed (Buy)
    cursor.execute("SELECT COUNT(*) as trades_placed FROM trades WHERE date(timestamp) = ?", (date_str,))
    trades_placed = cursor.fetchone()['trades_placed']
    
    # Realized PnL from filled Limit Sells
    cursor.execute("SELECT SUM(realized_pnl) as total_pnl FROM limit_orders WHERE status = 'filled' AND date(fill_timestamp) = ?", (date_str,))
    total_pnl = cursor.fetchone()['total_pnl'] or 0.0
    
    conn.close()
    
    return {
        "api_calls": api_calls,
        "trades_placed": trades_placed,
        "total_pnl": total_pnl
    }

def send_summary_notification(summary_text):
    targets = config.summary.get('targets', ["whatsapp"])
    for target in targets:
        try:
            subprocess.run(["hermes", "send", "--to", target, summary_text], check=True, capture_output=True)
            print(f"Daily summary sent to {target}")
        except Exception as e:
            print(f"Failed to send daily summary to {target}: {e}")
            log_system_event('error', f"Summary push failed for {target}: {e}")

def summary_loop():
    print("Starting Hermes Daily Summary Scheduler...")
    log_system_event('startup', 'Hermes Daily Summary Scheduler started')
    
    last_sent_date = None
    
    while True:
        try:
            if not config.summary.get('enabled', True):
                time.sleep(3600)
                continue
                
            now = datetime.now(NY_TZ)
            current_date_str = now.strftime("%Y-%m-%d")
            
            target_time_str = config.summary.get('time', '16:30')
            target_time = datetime.strptime(target_time_str, "%H:%M").time()
            
            # Check if it's past the target time and we haven't sent it today
            if now.time() >= target_time and last_sent_date != current_date_str:
                metrics = get_daily_metrics(current_date_str)
                
                # Format message
                sign = "+" if metrics['total_pnl'] >= 0 else ""
                summary_text = (
                    f"📊 *Hermes Daily Summary ({current_date_str})*\n\n"
                    f"• Realized P/L: {sign}${metrics['total_pnl']:.2f}\n"
                    f"• Trades Executed: {metrics['trades_placed']}\n"
                    f"• X API Calls Used: {metrics['api_calls']}"
                )
                
                send_summary_notification(summary_text)
                log_system_event('system', f"Daily summary generated and sent for {current_date_str}")
                
                last_sent_date = current_date_str
                
        except Exception as e:
            print(f"Error in summary loop: {e}")
            log_system_event('error', f"Summary loop error: {e}")
            
        # Check every 60 seconds
        time.sleep(60)

if __name__ == "__main__":
    summary_loop()
