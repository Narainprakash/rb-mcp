import hashlib
import uuid
import math
from datetime import datetime
from src.core.config import get_user_config
from src.core.db import get_connection, log_system_event
from src.core.time_utils import NY_TZ
from src.services.notifier import (
    notify_executed_live, 
    notify_executed_paper, 
    notify_limit_sell_placed,
    notify_error
)

# Circuit breaker: tracks consecutive execution errors per user, so one user's
# successes cannot reset another user's failure streak.
_consecutive_errors = {}

def run_notifications(notifications):
    """Fires deferred notifications. Always call this *after* committing - these
    do network I/O, and running them inside a transaction holds the write lock
    for the duration of an HTTP request or subprocess."""
    for notify in notifications:
        try:
            notify()
        except Exception as e:
            print(f"Notification failed: {e}")


def get_user_robinhood_account_id(user_id: int):
    """Returns the per-user Robinhood agentic account id, or None if unset."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT robinhood_account_id FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    return row['robinhood_account_id'] if row else None


def get_live_quote(ticker, expiry, strike, option_type, reference_price):
    """
    SIMULATED quote source. This is NOT market data.

    There is no Robinhood MCP integration yet, so quotes are derived
    deterministically from `reference_price` (the alert price for entries, the
    position's average cost for exits) plus a small drift that is a pure
    function of the contract and the current minute. Two consequences worth
    knowing: results are reproducible rather than random, and simulated P/L
    says nothing about how the strategy would have performed on real prices.

    When the MCP is wired in, replace this body with the real quote call and
    log it via log_api_call('robinhood', ...).
    """
    if reference_price is None or reference_price <= 0:
        raise ValueError(f"reference_price required for simulated quote of {ticker} {strike}{option_type}")

    # Deterministic drift in [-0.15, +0.15] keyed on contract + minute bucket.
    bucket = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M")
    seed = f"{ticker}|{expiry}|{strike}|{option_type}|{bucket}"
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    drift_pct = ((digest % 3001) / 10000.0) - 0.15

    mid = max(0.01, reference_price * (1 + drift_pct))
    spread = max(0.01, round(mid * 0.02, 2))
    return {"bid": round(mid - spread / 2, 2), "ask": round(mid + spread / 2, 2)}

def process_buy_fill(user_id: int, decision_id: int, ticker: str, expiry: str, strike: float, option_type: str, signal_action: str, fill_price: float, contracts: int, paper_mode: bool, order_id: str, conn=None):
    """
    Handles the post-fill logic for a buy order scoped to a specific user.
    Used by both immediate market buys and when a limit buy fills.
    It logs the trade to the specific user_id, updates their independent position ledger,
    and places a take-profit limit sell scoped to that user.

    Pass `conn` to join the caller's transaction - the monitor does this so the
    fill and the 'filled' stamp on the limit buy order commit atomically, and so
    a second connection never blocks on the caller's open write lock.

    Returns a list of zero-argument callables to invoke *after* the transaction
    commits. Notifications do network I/O and must not run inside it.
    """
    config = get_user_config(user_id)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    notifications = []
    try:
        cursor = conn.cursor()

        # 1. Log Trade
        cursor.execute("""
            INSERT INTO trades (user_id, decision_id, paper_mode, buy_order_id, fill_price, quantity, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, decision_id, paper_mode, order_id, fill_price, contracts, 'open'))
        trade_id = cursor.lastrowid

        notify_fill = notify_executed_paper if paper_mode else notify_executed_live
        notifications.append(lambda: notify_fill(user_id, contracts, ticker, strike, option_type, fill_price))

        # 2. Position Management
        cursor.execute("""
            SELECT id, total_quantity, average_cost FROM positions 
            WHERE user_id = ? AND ticker = ? AND expiry = ? AND strike = ? AND option_type = ? AND status = 'open'
        """, (user_id, ticker, expiry, strike, option_type))
        position_row = cursor.fetchone()
        
        if not position_row:
            # No open position for this contract. An ADD landing here means we
            # missed the original BTO (e.g. downtime), so flag it for review
            # rather than silently treating it as a fresh entry.
            add_without_parent = 1 if signal_action == "ADD" else 0
            cursor.execute("""
                INSERT INTO positions (user_id, ticker, expiry, strike, option_type, total_quantity, average_cost, status, add_without_parent)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """, (user_id, ticker, expiry, strike, option_type, contracts, fill_price, add_without_parent))
            position_id = cursor.lastrowid
            new_quantity = contracts
            new_avg_cost = fill_price
        else:
            # Add to the existing open position for this contract (ADD, or a
            # repeat BTO on a contract we already hold).
            position_id = position_row['id']
            old_qty = position_row['total_quantity']
            old_avg = position_row['average_cost']
            
            new_quantity = old_qty + contracts
            new_avg_cost = ((old_qty * old_avg) + (contracts * fill_price)) / new_quantity
            
            cursor.execute("""
                UPDATE positions 
                SET total_quantity = ?, average_cost = ?, updated_at = datetime('now', 'localtime')
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
            INSERT INTO limit_orders (user_id, position_id, sell_order_id, target_price, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (user_id, position_id, sell_order_id, target_sell_price))

        # Link the trade to its position so the lifecycle can close it later.
        cursor.execute("UPDATE trades SET position_id = ? WHERE id = ?", (position_id, trade_id))

        notifications.append(
            lambda: notify_limit_sell_placed(user_id, new_quantity, ticker, strike, option_type, target_sell_price, take_profit_pct)
        )

        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()

    if owns_conn:
        run_notifications(notifications)
        return []
    # Caller owns the transaction and fires these after it commits.
    return notifications


def execute_trade(user_id: int, decision_id: int, alert_id: int, ticker: str, expiry: str, strike: float, option_type: str, signal_action: str, decision_action: str, recommended_price: float):
    """
    Routes the execution based on the user-specific decision engine's output (market_buy or limit_buy).
    Implements a circuit breaker that engages the kill switch globally after N consecutive errors.
    """
    config = get_user_config(user_id)

    paper_mode = config.execution.get('paper_mode', True)
    if not paper_mode:
        # No real Robinhood MCP integration exists yet (quotes are simulated).
        # Fail safe into paper mode instead of silently faking a live order.
        # The per-user account id is resolved here so the routing target is
        # explicit once the MCP call is wired in.
        account_id = get_user_robinhood_account_id(user_id)
        detail = f"account_id={account_id}" if account_id else "no robinhood_account_id set for this user"
        log_system_event('error', f"Live execution requested but Robinhood MCP is not integrated ({detail}); forcing paper mode", user_id=user_id)
        notify_error("Executor", f"Live trading requested but Robinhood MCP is not integrated ({detail}); falling back to paper mode", user_id=user_id)
        paper_mode = True
    contracts = config.decision.get('contracts_per_signal', 1)
    breaker_limit = config.execution.get('error_circuit_breaker_count', 3)
    
    order_id = f"sim_{uuid.uuid4().hex[:8]}" if paper_mode else "real_mcp_order_id"

    try:
        if decision_action == "market_buy":
            # Cap the market buy slippage using max_price
            tolerance_pct = config.decision.get('price_tolerance_pct', 10)
            max_price = round(recommended_price * (1 + (tolerance_pct / 100)), 2)
            
            # In live mode, this MUST be routed to Robinhood as a Limit Buy at max_price.
            # In paper mode, we simulate checking the current ask:
            quote = get_live_quote(ticker, expiry, strike, option_type, recommended_price)
            if quote['ask'] <= max_price:
                # Immediate fill. process_buy_fill owns its transaction here and
                # fires its own notifications after committing.
                fill_price = quote['ask']
                process_buy_fill(user_id, decision_id, ticker, expiry, strike, option_type, signal_action, fill_price, contracts, paper_mode, order_id)
            else:
                # Spiked above max_price between decision and execution! Drop to pending limit order.
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO limit_buy_orders (user_id, decision_id, alert_id, buy_order_id, target_price, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """, (user_id, decision_id, alert_id, order_id, max_price, contracts))
                    conn.commit()
                finally:
                    conn.close()
                
                from src.services.notifier import notify_limit_buy_placed
                notify_limit_buy_placed(user_id, contracts, ticker, strike, option_type, max_price, tolerance_pct)
            
        elif decision_action == "limit_buy":
            # Place a limit buy order at a discount
            discount_pct = config.decision.get('limit_buy_discount_pct', 20)
            target_buy_price = round(recommended_price * (1 - (discount_pct / 100)), 2)
            
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO limit_buy_orders (user_id, decision_id, alert_id, buy_order_id, target_price, quantity, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """, (user_id, decision_id, alert_id, order_id, target_buy_price, contracts))
                conn.commit()
            finally:
                conn.close()
            
            from src.services.notifier import notify_limit_buy_placed
            notify_limit_buy_placed(user_id, contracts, ticker, strike, option_type, target_buy_price, discount_pct)
        
        # Success: reset this user's consecutive error counter
        _consecutive_errors[user_id] = 0

    except Exception as e:
        errors = _consecutive_errors.get(user_id, 0) + 1
        _consecutive_errors[user_id] = errors
        log_system_event('error', f"Execution error #{errors}: {e}", user_id=user_id)
        notify_error("Executor", f"Trade execution failed ({errors}/{breaker_limit}): {e}", user_id=user_id)

        if errors >= breaker_limit:
            log_system_event('kill_switch', f"Circuit breaker tripped after {errors} consecutive execution errors", user_id=user_id)
            from src.core.security import engage_kill_switch
            engage_kill_switch()
            notify_error("Executor", f"CIRCUIT BREAKER: Kill switch engaged after {errors} consecutive errors", user_id=user_id)


