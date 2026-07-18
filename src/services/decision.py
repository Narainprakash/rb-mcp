import math
from src.core.config import config
from src.core.db import get_connection
from src.services.notifier import notify_skipped

def check_daily_spend_limit(requested_spend: float) -> bool:
    """Checks if requested spend exceeds max_daily_spend_usd limit"""
    limit = config.decision.get('max_daily_spend_usd', 500)
    # Get total spend today
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sum(fill_price * quantity * 100) as total_spend 
        FROM trades 
        WHERE date(timestamp) = date('now', 'localtime')
    """)
    row = cursor.fetchone()
    total_spend = row['total_spend'] if row['total_spend'] else 0.0
    conn.close()
    
    return (total_spend + requested_spend) <= limit

def check_open_positions_limit() -> bool:
    """Checks if we have reached the maximum open positions limit"""
    limit = config.decision.get('max_open_positions', 10)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) as count FROM positions WHERE status = 'open'")
    row = cursor.fetchone()
    count = row['count']
    conn.close()
    return count < limit

def compute_decision(alert_id: int, recommended_price: float, live_ask: float):
    """
    Evaluates price tolerance against the live ask price.
    Returns (action_taken, reasoning)
    """
    tolerance_pct = config.decision.get('price_tolerance_pct', 10)
    max_price = recommended_price * (1 + (tolerance_pct / 100))
    
    # Check max per-trade spend (assuming 1 contract = 100 multiplier)
    contracts = config.decision.get('contracts_per_signal', 1)
    estimated_spend = live_ask * contracts * 100
    per_trade_max = config.decision.get('per_trade_max_spend_usd', 500)
    
    if estimated_spend > per_trade_max:
        reason = f"estimated spend ${estimated_spend} exceeds per_trade limit ${per_trade_max}"
        return "skip", reason
        
    if not check_daily_spend_limit(estimated_spend):
        reason = f"estimated spend ${estimated_spend} would exceed daily max"
        return "skip", reason
        
    if not check_open_positions_limit():
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
        notify_skipped(live_ask, recommended_price, tolerance_pct)
        return "skip", reason

def log_decision(alert_id, recommended_price, observed_price, action_taken, reasoning):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO decisions (alert_id, recommended_price, observed_price, action_taken, reasoning)
        VALUES (?, ?, ?, ?, ?)
    """, (alert_id, recommended_price, observed_price, action_taken, reasoning))
    decision_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return decision_id
