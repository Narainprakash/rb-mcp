import time
import subprocess
from datetime import datetime

from src.core.config import config
from src.core.db import get_connection, log_system_event
from src.core.time_utils import NY_TZ, get_today_ny_bounds

def get_daily_metrics(date_str):
    """Fetches PnL, Trades, Alerts, Open Positions, API calls, and Win Rate for the given NY date string."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        start_utc, end_utc = get_today_ny_bounds()
        
        # Monthly API Calls
        now_ny = datetime.now(NY_TZ)
        start_of_month = f"{now_ny.year}-{now_ny.month:02d}-01 00:00:00"
        cursor.execute("SELECT COUNT(*) as monthly_api_calls FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,))
        monthly_api_calls = cursor.fetchone()['monthly_api_calls']
        
        # Trades executed (Buy)
        cursor.execute("SELECT COUNT(*) as trades_placed FROM trades WHERE timestamp >= ? AND timestamp < ?", (start_utc, end_utc))
        trades_placed = cursor.fetchone()['trades_placed']
        
        # Realized PnL from filled Limit Sells
        cursor.execute("SELECT SUM(realized_pnl) as total_pnl FROM limit_orders WHERE status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ?", (start_utc, end_utc))
        total_pnl = cursor.fetchone()['total_pnl'] or 0.0
        
        # Win Rate
        cursor.execute("SELECT COUNT(*) as profitable FROM limit_orders WHERE status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ? AND realized_pnl > 0", (start_utc, end_utc))
        profitable = cursor.fetchone()['profitable']
        
        cursor.execute("SELECT COUNT(*) as total_closed FROM limit_orders WHERE status = 'filled' AND fill_timestamp >= ? AND fill_timestamp < ?", (start_utc, end_utc))
        total_closed = cursor.fetchone()['total_closed']
        win_rate = (profitable / total_closed * 100) if total_closed > 0 else 0.0
        
        # Alerts parsed today
        cursor.execute("SELECT COUNT(*) as alerts_parsed FROM alerts WHERE timestamp >= ? AND timestamp < ?", (start_utc, end_utc))
        alerts_parsed = cursor.fetchone()['alerts_parsed']
        
        # Current open positions
        cursor.execute("SELECT COUNT(*) as open_positions FROM positions WHERE status = 'open'")
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
                paper_mode = config.execution.get("paper_mode", True)
                mode_str = "PAPER" if paper_mode else "LIVE"
                api_limit = config.polling.get("monthly_api_call_ceiling", 10000)
                
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
