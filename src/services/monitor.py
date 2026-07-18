import time
from datetime import datetime
import pytz
from src.core.config import config
from src.core.db import get_connection
from src.services.executor import get_live_quote
from src.services.notifier import notify_limit_sell_filled

NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

def process_open_orders():
    """
    Monitors open limit sell and limit buy orders.
    In paper mode, simulates fills by checking if the market bid/ask reaches target_price.
    Handles the 15:50 ET 0DTE market sell cutoff for sells, and cancels for buys.
    """
    paper_mode = config.execution.get('paper_mode', True)
    cutoff_time_str = config.execution.get('zero_dte_market_sell_cutoff', '15:50')
    cutoff_time = datetime.strptime(cutoff_time_str, "%H:%M").time()
    
    now = datetime.now(NY_TZ)
    current_time = now.time()
    today_str = now.strftime("%Y-%m-%d")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # ---------------------------------------------------------
    # 1. PROCESS LIMIT SELL ORDERS
    # ---------------------------------------------------------
    cursor.execute("""
        SELECT l.id as limit_id, l.target_price, l.sell_order_id,
               p.id as position_id, p.ticker, p.expiry, p.strike, p.option_type, p.total_quantity, p.average_cost
        FROM limit_orders l
        JOIN positions p ON l.position_id = p.id
        WHERE l.status = 'pending'
    """)
    sell_orders = cursor.fetchall()
    
    for order in sell_orders:
        is_0dte = (order['expiry'] == today_str)
        
        # Check 0DTE Cutoff rule
        if is_0dte and current_time >= cutoff_time:
            # Force Market Sell
            quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'])
            fill_price = quote['bid'] # Market sell hits the bid
            
            pnl_dollars = round((fill_price - order['average_cost']) * order['total_quantity'] * 100, 2)
            pnl_pct = round(((fill_price / order['average_cost']) - 1) * 100, 2)
            
            cursor.execute("""
                UPDATE limit_orders 
                SET status = 'filled', fill_timestamp = CURRENT_TIMESTAMP, realized_pnl = ?
                WHERE id = ?
            """, (pnl_dollars, order['limit_id']))
            
            cursor.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (order['position_id'],))
            
            notify_limit_sell_filled(order['total_quantity'], order['ticker'], order['strike'], 
                                     order['option_type'], fill_price, pnl_dollars, pnl_pct)
            continue
            
        # Check for limit fill (Paper Mode Simulation)
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
            # Live Mode: Poll Robinhood MCP for order status
            pass

    # ---------------------------------------------------------
    # 2. PROCESS LIMIT BUY ORDERS
    # ---------------------------------------------------------
    cursor.execute("""
        SELECT b.id as buy_id, b.decision_id, b.buy_order_id, b.target_price, b.quantity,
               a.ticker, a.expiry, a.strike, a.option_type, a.action as signal_action
        FROM limit_buy_orders b
        JOIN alerts a ON b.alert_id = a.id
        WHERE b.status = 'pending'
    """)
    buy_orders = cursor.fetchall()

    for order in buy_orders:
        is_0dte = (order['expiry'] == today_str)

        # Check 0DTE Cutoff rule - cancel buys at end of day
        if is_0dte and current_time >= cutoff_time:
            cursor.execute("UPDATE limit_buy_orders SET status = 'cancelled' WHERE id = ?", (order['buy_id'],))
            continue

        if paper_mode:
            quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'])
            # Buy order fills if ask drops to target price
            if quote['ask'] <= order['target_price']:
                # Import here to avoid circular imports if any
                from src.services.executor import process_buy_fill
                
                # We fill at the target price (limit price)
                fill_price = order['target_price']
                
                # Execute the position update / take-profit logic
                process_buy_fill(
                    decision_id=order['decision_id'],
                    ticker=order['ticker'],
                    expiry=order['expiry'],
                    strike=order['strike'],
                    option_type=order['option_type'],
                    signal_action=order['signal_action'],
                    fill_price=fill_price,
                    contracts=order['quantity'],
                    paper_mode=paper_mode,
                    order_id=order['buy_order_id']
                )

                # Mark as filled
                cursor.execute("""
                    UPDATE limit_buy_orders 
                    SET status = 'filled', fill_timestamp = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (order['buy_id'],))
        else:
            # Live Mode: Poll Robinhood MCP for order status
            pass
            
    conn.commit()
    conn.close()
