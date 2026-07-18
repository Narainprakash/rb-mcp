import threading
import time
from src.core.db import init_db, get_connection
from src.services.poller import start_poller
from src.services.decision import compute_decision, log_decision
from src.services.executor import execute_trade, get_live_quote
from src.services.monitor import process_open_orders
from src.core.security import check_kill_switch

def trade_loop():
    """
    Independent loop that reads unprocessed alerts from the database,
    makes trading decisions, executes them, and monitors open orders.
    """
    print("Starting Hermes Trade & Execution Loop...")
    while True:
        try:
            # 1. Check Kill Switch
            check_kill_switch()
            
            conn = get_connection()
            cursor = conn.cursor()
            
            # 2. Find alerts that haven't been decisioned yet
            cursor.execute("""
                SELECT a.* FROM alerts a
                LEFT JOIN decisions d ON a.id = d.alert_id
                WHERE a.parse_status = 'success' AND d.id IS NULL
                ORDER BY a.timestamp ASC
            """)
            pending_alerts = cursor.fetchall()
            
            for alert in pending_alerts:
                print(f"Processing decision for alert ID {alert['id']} (${alert['ticker']} {alert['action']})")
                
                # Fetch live quote for the decision engine
                quote = get_live_quote(alert['ticker'], alert['expiry'], alert['strike'], alert['option_type'])
                live_ask = quote['ask']
                
                # Compute decision based on price tolerance and risk limits
                action, reasoning = compute_decision(alert['id'], alert['price'], live_ask)
                
                # Log decision
                decision_id = log_decision(alert['id'], alert['price'], live_ask, action, reasoning)
                
                # Execute if within tolerance and limits
                if action in ("market_buy", "limit_buy"):
                    execute_trade(
                        decision_id=decision_id,
                        alert_id=alert['id'],
                        ticker=alert['ticker'], 
                        expiry=alert['expiry'], 
                        strike=alert['strike'], 
                        option_type=alert['option_type'], 
                        signal_action=alert['action'],
                        decision_action=action,
                        recommended_price=alert['price']
                    )
            
            # 3. Monitor and process open orders (limit sells and limit buys)
            process_open_orders()
            
            conn.close()
            
        except Exception as e:
            print(f"Error in trade loop: {e}")
            
        # Run trade loop frequently to catch limit sell fills quickly
        time.sleep(2)

if __name__ == "__main__":
    print("Initializing Database...")
    init_db()
    
    print("Starting Background Threads...")
    # Start poller thread
    poller_thread = threading.Thread(target=start_poller, daemon=True)
    poller_thread.start()
    
    # Start trade loop in main thread
    trade_loop()
