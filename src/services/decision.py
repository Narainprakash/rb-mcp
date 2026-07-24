import math
from src.core.config import get_user_config
from src.core.db import get_connection
from src.services.notifier import notify_skipped
from src.core.time_utils import get_today_ny_bounds

def check_daily_spend_limit(user_id: int, requested_spend: float) -> bool:
    """Checks if requested spend exceeds max_daily_spend_usd limit"""
    config = get_user_config(user_id)
    limit = config.decision.get('max_daily_spend_usd', 500)
    
    start_ny, end_ny = get_today_ny_bounds()
    
    # Get total spend today
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # 1. Filled Trades
        cursor.execute("""
            SELECT sum(fill_price * quantity * 100) as total_spend 
            FROM trades 
            WHERE user_id = ? AND timestamp >= ? AND timestamp < ?
        """, (user_id, start_ny, end_ny))
        row = cursor.fetchone()
        filled_spend = row['total_spend'] if row['total_spend'] else 0.0
        
        # 2. Pending Limit Buys (Locked Capital)
        cursor.execute("""
            SELECT sum(target_price * quantity * 100) as pending_spend
            FROM limit_buy_orders
            WHERE user_id = ? AND status = 'pending' AND created_at >= ? AND created_at < ?
        """, (user_id, start_ny, end_ny))
        row2 = cursor.fetchone()
        pending_spend = row2['pending_spend'] if row2['pending_spend'] else 0.0
        
        total_spend = filled_spend + pending_spend
        
    finally:
        conn.close()
    
    return (total_spend + requested_spend) <= limit

def check_open_positions_limit(user_id: int) -> bool:
    """Checks if we have reached the maximum open positions limit"""
    config = get_user_config(user_id)
    limit = config.decision.get('max_open_positions', 10)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as count FROM positions WHERE user_id = ? AND status = 'open'", (user_id,))
        row = cursor.fetchone()
        count = row['count']
    finally:
        conn.close()
    return count < limit

def check_position_spend_limit(user_id: int, ticker: str, expiry: str, strike: float, option_type: str, requested_spend: float) -> bool:
    """Checks if adding to this position exceeds the max_spend_per_position_usd limit"""
    config = get_user_config(user_id)
    limit = config.decision.get('max_spend_per_position_usd', 1000)
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT average_cost, total_quantity 
            FROM positions 
            WHERE user_id = ? AND ticker = ? AND expiry = ? AND strike = ? AND option_type = ? AND status = 'open'
        """, (user_id, ticker, expiry, strike, option_type))
        row = cursor.fetchone()
    finally:
        conn.close()
    
    if row:
        current_position_value = row['average_cost'] * row['total_quantity'] * 100
    else:
        current_position_value = 0.0
        
    return (current_position_value + requested_spend) <= limit

def compute_decision(user_id: int, alert_id: int, ticker: str, expiry: str, strike: float, option_type: str, recommended_price: float, live_ask: float):
    """
    Evaluates price tolerance against the live ask price.
    Returns (action_taken, reasoning)
    """
    config = get_user_config(user_id)
    tolerance_pct = config.decision.get('price_tolerance_pct', 10)
    max_price = recommended_price * (1 + (tolerance_pct / 100))
    
    # Check max per-trade spend (assuming 1 contract = 100 multiplier)
    contracts = config.decision.get('contracts_per_signal', 1)
    estimated_spend = live_ask * contracts * 100
    per_trade_max = config.decision.get('per_trade_max_spend_usd', 500)
    
    if estimated_spend > per_trade_max:
        reason = f"estimated spend ${estimated_spend} exceeds per_trade limit ${per_trade_max}"
        return "skip", reason
        
    if not check_daily_spend_limit(user_id, estimated_spend):
        reason = f"estimated spend ${estimated_spend} would exceed daily max"
        return "skip", reason
        
    if not check_position_spend_limit(user_id, ticker, expiry, strike, option_type, estimated_spend):
        reason = f"estimated spend ${estimated_spend} would exceed max spend per position"
        return "skip", reason
        
    if not check_open_positions_limit(user_id):
        reason = "maximum open positions limit reached"
        return "skip", reason

    if live_ask < recommended_price:
        reason = f"ask {live_ask} is below recommended {recommended_price}. Will place discount limit buy."
        return "limit_buy", reason
    elif live_ask <= max_price:
        reason = f"ask {live_ask} is within tolerance of {max_price} (rec {recommended_price})"
        return "market_buy", reason
    else:
        reason = f"ask {live_ask} exceeds {max_price} (+{tolerance_pct}% tolerance)"
        notify_skipped(user_id, live_ask, recommended_price, tolerance_pct)
        return "skip", reason

def log_decision(user_id: int, alert_id: int, recommended_price: float, observed_price: float, action_taken: str, reasoning: str):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO decisions (user_id, alert_id, recommended_price, observed_price, action_taken, reasoning)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, alert_id, recommended_price, observed_price, action_taken, reasoning))
        decision_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return decision_id

