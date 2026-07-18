import random
import uuid
import math
from src.core.config import config
from src.core.db import get_connection
from src.services.notifier import (
    notify_executed_live, 
    notify_executed_paper, 
    notify_limit_sell_placed
)

def get_live_quote(ticker, expiry, strike, option_type):
    """
    Mock function to get a live quote.
    If paper_mode is false, this would invoke the Robinhood MCP tool.
    """
    # For simulation, just return a fake bid/ask around a random price
    base = random.uniform(1.0, 3.0)
    bid = round(base, 2)
    ask = round(base + 0.05, 2)
    return {"bid": bid, "ask": ask}

def execute_trade(decision_id: int, ticker: str, expiry: str, strike: float, option_type: str, action: str):
    """
    Executes the trade (paper or live).
    Handles 'BTO' (new position) and 'ADD' (averaging in).
    Places subsequent Limit Sell order.
    """
    paper_mode = config.execution.get('paper_mode', True)
    contracts = config.decision.get('contracts_per_signal', 1)
    
    # 1. Fetch quote
    quote = get_live_quote(ticker, expiry, strike, option_type)
    fill_price = quote['ask']
    order_id = f"sim_{uuid.uuid4().hex[:8]}" if paper_mode else "real_mcp_order_id"
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 2. Log Trade
    cursor.execute("""
        INSERT INTO trades (decision_id, paper_mode, buy_order_id, fill_price, quantity, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (decision_id, paper_mode, order_id, fill_price, contracts, 'open'))
    
    if paper_mode:
        notify_executed_paper(contracts, ticker, strike, option_type, fill_price)
    else:
        notify_executed_live(contracts, ticker, strike, option_type, fill_price)

    # 3. Position Management
    cursor.execute("""
        SELECT id, total_quantity, average_cost FROM positions 
        WHERE ticker = ? AND expiry = ? AND strike = ? AND option_type = ? AND status = 'open'
    """, (ticker, expiry, strike, option_type))
    position_row = cursor.fetchone()
    
    if action == "BTO" or not position_row:
        # Create new position
        cursor.execute("""
            INSERT INTO positions (ticker, expiry, strike, option_type, total_quantity, average_cost, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
        """, (ticker, expiry, strike, option_type, contracts, fill_price))
        position_id = cursor.lastrowid
        new_quantity = contracts
        new_avg_cost = fill_price
    else:
        # Update existing position (ADD)
        position_id = position_row['id']
        old_qty = position_row['total_quantity']
        old_avg = position_row['average_cost']
        
        new_quantity = old_qty + contracts
        new_avg_cost = ((old_qty * old_avg) + (contracts * fill_price)) / new_quantity
        
        cursor.execute("""
            UPDATE positions 
            SET total_quantity = ?, average_cost = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_quantity, new_avg_cost, position_id))
        
        # Cancel old limit sell orders for this position
        cursor.execute("""
            UPDATE limit_orders SET status = 'cancelled' 
            WHERE position_id = ? AND status = 'pending'
        """, (position_id,))
    
    # 4. Place Take-Profit Limit Sell
    take_profit_pct = config.decision.get('take_profit_pct', 20)
    target_sell_price = new_avg_cost * (1 + (take_profit_pct / 100))
    # CRITICAL: Round to exactly 2 decimal places to avoid tick-size rejections
    target_sell_price = round(target_sell_price, 2)
    
    sell_order_id = f"sim_sell_{uuid.uuid4().hex[:8]}" if paper_mode else "real_mcp_sell_id"
    
    cursor.execute("""
        INSERT INTO limit_orders (position_id, sell_order_id, target_price, status)
        VALUES (?, ?, ?, 'pending')
    """, (position_id, sell_order_id, target_sell_price))
    
    conn.commit()
    conn.close()
    
    notify_limit_sell_placed(new_quantity, ticker, strike, option_type, target_sell_price, take_profit_pct)
