import time
from datetime import datetime
import pytz
from src.core.config import config
from src.core.db import get_connection
from src.services.executor import get_live_quote
from src.services.notifier import notify_limit_sell_filled

NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

def process_limit_sells():
    """
    Monitors open limit sell orders.
    In paper mode, simulates fills by checking if the market bid >= target_price.
    Handles the 15:50 ET 0DTE market sell cutoff.
    """
    paper_mode = config.execution.get('paper_mode', True)
    cutoff_time_str = config.execution.get('zero_dte_market_sell_cutoff', '15:50')
    cutoff_time = datetime.strptime(cutoff_time_str, "%H:%M").time()
    
    now = datetime.now(NY_TZ)
    current_time = now.time()
    today_str = now.strftime("%Y-%m-%d")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all pending limit orders with their position details
    cursor.execute("""
        SELECT l.id as limit_id, l.target_price, l.sell_order_id,
               p.id as position_id, p.ticker, p.expiry, p.strike, p.option_type, p.total_quantity, p.average_cost
        FROM limit_orders l
        JOIN positions p ON l.position_id = p.id
        WHERE l.status = 'pending'
    """)
    orders = cursor.fetchall()
    
    for order in orders:
        is_0dte = (order['expiry'] == today_str)
        
        # 1. Check 0DTE Cutoff rule
        if is_0dte and current_time >= cutoff_time:
            # Force Market Sell
            quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'])
            fill_price = quote['bid'] # Market sell hits the bid
            
            pnl_dollars = round((fill_price - order['average_cost']) * order['total_quantity'] * 100, 2)
            pnl_pct = round(((fill_price / order['average_cost']) - 1) * 100, 2)
            
            # Update limit order status to filled (via market sell override)
            cursor.execute("""
                UPDATE limit_orders 
                SET status = 'filled', fill_timestamp = CURRENT_TIMESTAMP, realized_pnl = ?
                WHERE id = ?
            """, (pnl_dollars, order['limit_id']))
            
            # Close position
            cursor.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (order['position_id'],))
            
            notify_limit_sell_filled(order['total_quantity'], order['ticker'], order['strike'], 
                                     order['option_type'], fill_price, pnl_dollars, pnl_pct)
            continue
            
        # 2. Check for limit fill (Paper Mode Simulation)
        if paper_mode:
            quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'])
            if quote['bid'] >= order['target_price']:
                # Simulate Fill
                pnl_dollars = round((order['target_price'] - order['average_cost']) * order['total_quantity'] * 100, 2)
                pnl_pct = round(((order['target_price'] / order['average_cost']) - 1) * 100, 2)
                
                cursor.execute("""
                    UPDATE limit_orders 
                    SET status = 'filled', fill_timestamp = CURRENT_TIMESTAMP, realized_pnl = ?
                    WHERE id = ?
                """, (pnl_dollars, order['limit_id']))
                
                cursor.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (order['position_id'],))
                
                notify_limit_sell_filled(order['total_quantity'], order['ticker'], order['strike'], 
                                         order['option_type'], order['target_price'], pnl_dollars, pnl_pct)
        else:
            # Live Mode: Here you would poll the Robinhood MCP for order status
            # status = check_mcp_order_status(order['sell_order_id'])
            pass
            
    conn.commit()
    conn.close()
