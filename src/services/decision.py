import math
from src.core.config import get_user_config
from src.core.db import get_connection
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

def check_daily_loss_limit(user_id: int) -> tuple:
    """Realized loss today vs max_daily_loss_usd. Returns (within_limit, loss).

    Spend caps limit what gets deployed, not what gets lost - with no stop-loss
    by design, a full day's spend can become a full day's loss. This is the
    other half of that guardrail.
    """
    config = get_user_config(user_id)
    limit = config.decision.get('max_daily_loss_usd', 0)
    if not limit or limit <= 0:
        return True, 0.0  # disabled

    start_ny, end_ny = get_today_ny_bounds()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
            FROM limit_orders
            WHERE user_id = ? AND status = 'filled'
              AND fill_timestamp >= ? AND fill_timestamp < ?
        """, (user_id, start_ny, end_ny))
        pnl = cursor.fetchone()['pnl'] or 0.0
    finally:
        conn.close()

    loss = -pnl if pnl < 0 else 0.0
    return loss < limit, loss


def resolve_contracts(user_id: int, live_ask: float) -> int:
    """Position size for this signal.

    In 'contracts' mode (default) this is a fixed count, which means risk per
    signal swings with the option's price - 1 contract is $50 at $0.50 and $500
    at $5.00. In 'dollars' mode the count is derived from risk_per_signal_usd so
    exposure is normalised across signals. Returns 0 when the budget cannot
    cover a single contract.
    """
    config = get_user_config(user_id)
    if config.decision.get('sizing_mode', 'contracts') != 'dollars':
        return config.decision.get('contracts_per_signal', 1)

    budget = config.decision.get('risk_per_signal_usd', 0)
    cost_per_contract = live_ask * 100
    if cost_per_contract <= 0:
        return 0
    return int(budget // cost_per_contract)


def compute_decision(user_id: int, alert_id: int, ticker: str, expiry: str, strike: float, option_type: str, recommended_price: float, live_ask: float, trade_style: str = None):
    """
    Evaluates price tolerance against the live ask price.
    Returns (action_taken, reasoning, contracts)
    """
    config = get_user_config(user_id)
    tolerance_pct = config.decision.get('price_tolerance_pct', 10)
    max_price = recommended_price * (1 + (tolerance_pct / 100))

    # Callers notify on every "skip" - see trade_loop. Reasons are formatted to
    # 2dp because they are surfaced verbatim in notifications and the dashboard.

    # Per-user soft pause: stop opening new positions but keep managing exits.
    # Distinct from the global HALT file, which stops exit management too.
    if not config.decision.get('trading_enabled', True):
        return "skip", "trading is paused for this user (trading_enabled: false)", 0

    within_loss_limit, loss_today = check_daily_loss_limit(user_id)
    if not within_loss_limit:
        limit = config.decision.get('max_daily_loss_usd', 0)
        return "skip", f"realized loss today ${loss_today:.2f} has reached the daily loss limit ${limit:.2f}", 0

    # Ticker allow/block lists (empty allow list means "all tickers").
    allowed = config.decision.get('allowed_tickers') or []
    blocked = config.decision.get('blocked_tickers') or []
    if allowed and ticker.upper() not in [t.upper() for t in allowed]:
        return "skip", f"{ticker} is not in the allowed ticker list", 0
    if ticker.upper() in [t.upper() for t in blocked]:
        return "skip", f"{ticker} is in the blocked ticker list", 0

    # Trade-style filter, e.g. opt out of 0DTE entirely.
    skip_styles = [s.upper() for s in (config.decision.get('skip_trade_styles') or [])]
    signal_styles = [s.strip().upper() for s in (trade_style or "").split(",") if s.strip()]
    matched = [s for s in signal_styles if s in skip_styles]
    if matched:
        return "skip", f"trade style {'/'.join(matched)} is excluded by skip_trade_styles", 0

    contracts = resolve_contracts(user_id, live_ask)
    if contracts < 1:
        budget = config.decision.get('risk_per_signal_usd', 0)
        return "skip", f"risk budget ${budget:.2f} cannot cover one contract at {live_ask:.2f}", 0

    estimated_spend = live_ask * contracts * 100
    per_trade_max = config.decision.get('per_trade_max_spend_usd', 500)

    if estimated_spend > per_trade_max:
        reason = f"estimated spend ${estimated_spend:.2f} exceeds per_trade limit ${per_trade_max:.2f}"
        return "skip", reason, 0

    if not check_daily_spend_limit(user_id, estimated_spend):
        reason = f"estimated spend ${estimated_spend:.2f} would exceed daily max"
        return "skip", reason, 0

    if not check_position_spend_limit(user_id, ticker, expiry, strike, option_type, estimated_spend):
        reason = f"estimated spend ${estimated_spend:.2f} would exceed max spend per position"
        return "skip", reason, 0

    if not check_open_positions_limit(user_id):
        reason = "maximum open positions limit reached"
        return "skip", reason, 0

    if live_ask < recommended_price:
        reason = f"ask {live_ask:.2f} is below recommended {recommended_price:.2f}. Will place discount limit buy."
        return "limit_buy", reason, contracts
    elif live_ask <= max_price:
        reason = f"ask {live_ask:.2f} is within tolerance of {max_price:.2f} (rec {recommended_price:.2f})"
        return "market_buy", reason, contracts
    else:
        reason = f"ask {live_ask:.2f} exceeds {max_price:.2f} (+{tolerance_pct}% tolerance)"
        return "skip", reason, 0

def log_decision(user_id: int, alert_id: int, recommended_price: float, observed_price: float, action_taken: str, reasoning: str, latency_sec: float = None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO decisions (user_id, alert_id, recommended_price, observed_price, action_taken, reasoning, latency_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, alert_id, recommended_price, observed_price, action_taken, reasoning, latency_sec))
        decision_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return decision_id

