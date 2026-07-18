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

def process_buy_fill(decision_id: int, ticker: str, expiry: str, strike: float, option_type: str, signal_action: str, fill_price: float, contracts: int, paper_mode: bool, order_id: str):
    """
    Handles the post-fill logic for a buy order: logs trade, updates position, places take-profit limit sell.
    Used by both immediate market buys and when a limit buy fills.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Log Trade
    cursor.execute("""
        INSERT INTO trades (decision_id, paper_mode, buy_order_id, fill_price, quantity, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (decision_id, paper_mode, order_id, fill_price, contracts, 'open'))
    
    if paper_mode:
        notify_executed_paper(contracts, ticker, strike, option_type, fill_price)
    else:
        notify_executed_live(contracts, ticker, strike, option_type, fill_price)

    # 2. Position Management
    cursor.execute("""
        SELECT id, total_quantity, average_cost FROM positions 
        WHERE ticker = ? AND expiry = ? AND strike = ? AND option_type = ? AND status = 'open'
    """, (ticker, expiry, strike, option_type))
    position_row = cursor.fetchone()
    
    if signal_action == "BTO" or not position_row:
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
    
    # 3. Place Take-Profit Limit Sell
    take_profit_pct = config.decision.get('take_profit_pct', 20)
    target_sell_price = new_avg_cost * (1 + (take_profit_pct / 100))
    target_sell_price = round(target_sell_price, 2)
    
    sell_order_id = f"sim_sell_{uuid.uuid4().hex[:8]}" if paper_mode else "real_mcp_sell_id"
    
    cursor.execute("""
        INSERT INTO limit_orders (position_id, sell_order_id, target_price, status)
        VALUES (?, ?, ?, 'pending')
    """, (position_id, sell_order_id, target_sell_price))
    
    conn.commit()
    conn.close()
    
    notify_limit_sell_placed(new_quantity, ticker, strike, option_type, target_sell_price, take_profit_pct)


def execute_trade(decision_id: int, alert_id: int, ticker: str, expiry: str, strike: float, option_type: str, signal_action: str, decision_action: str, recommended_price: float):
    """
    Routes the execution based on the decision engine's output (market_buy or limit_buy).
    """
    paper_mode = config.execution.get('paper_mode', True)
    contracts = config.decision.get('contracts_per_signal', 1)
    
    order_id = f"sim_{uuid.uuid4().hex[:8]}" if paper_mode else "real_mcp_order_id"

    if decision_action == "market_buy":
        # Immediate fill at live ask
        quote = get_live_quote(ticker, expiry, strike, option_type)
        fill_price = quote['ask']
        process_buy_fill(decision_id, ticker, expiry, strike, option_type, signal_action, fill_price, contracts, paper_mode, order_id)
        
    elif decision_action == "limit_buy":
        # Place a limit buy order at a discount
        discount_pct = config.decision.get('limit_buy_discount_pct', 20)
        target_buy_price = round(recommended_price * (1 - (discount_pct / 100)), 2)
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO limit_buy_orders (decision_id, alert_id, buy_order_id, target_price, quantity, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (decision_id, alert_id, order_id, target_buy_price, contracts))
        conn.commit()
        conn.close()
        
        from src.services.notifier import notify_limit_buy_placed
        notify_limit_buy_placed(contracts, ticker, strike, option_type, target_buy_price, discount_pct)
