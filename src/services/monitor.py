import time
from datetime import datetime, timedelta
import pytz
from src.core.config import get_user_config
from src.core.db import get_connection, log_system_event
from src.core.time_utils import NY_TZ
from src.services.executor import get_live_quote, run_notifications
from src.services.notifier import notify_limit_sell_filled

def _log_event(cursor, event_type, message, user_id=None):
    """Writes a system_event on the caller's cursor. Using log_system_event()
    here would open a second connection while this transaction holds the write
    lock, which under WAL blocks until the busy timeout and then throws."""
    cursor.execute(
        "INSERT INTO system_events (user_id, event_type, message) VALUES (?, ?, ?)",
        (user_id, event_type, message)
    )

def _close_position(cursor, position_id, status='closed'):
    """Closes a position and the trades that make it up, so trades.status stops
    disagreeing with positions.status."""
    cursor.execute("UPDATE positions SET status = ? WHERE id = ?", (status, position_id))
    cursor.execute("UPDATE trades SET status = ? WHERE position_id = ?", (status, position_id))

def process_open_orders():
    """
    Monitors open limit sell and limit buy orders across all users.
    In paper mode, simulates fills by checking if the market bid/ask reaches target_price.
    Handles the user-specific 0DTE market sell cutoff for sells, and cancels for buys.

    Everything runs in one transaction on one connection: nested connections
    deadlock against our own write lock. Notifications are collected and fired
    only after the commit, since they do network I/O.
    """
    now = datetime.now(NY_TZ)
    current_time = now.time()
    today_str = now.strftime("%Y-%m-%d")

    notifications = []
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # ---------------------------------------------------------
        # 0. RECONCILE EXPIRED POSITIONS
        # Options past their expiry can never fill their take-profit. Without
        # this they stay 'open' forever, consuming max_open_positions and
        # inflating capital-at-risk.
        # ---------------------------------------------------------
        cursor.execute("""
            SELECT id, user_id, ticker, expiry, strike, option_type, total_quantity, average_cost
            FROM positions WHERE status = 'open' AND expiry < ?
        """, (today_str,))
        for pos in cursor.fetchall():
            realized = round(-pos['average_cost'] * pos['total_quantity'] * 100, 2)
            cursor.execute("""
                UPDATE limit_orders SET status = 'cancelled'
                WHERE position_id = ? AND status = 'pending'
            """, (pos['id'],))
            _close_position(cursor, pos['id'], status='expired')
            _log_event(
                cursor,
                'system',
                f"Position #{pos['id']} {pos['ticker']} {pos['expiry']} {pos['strike']}{pos['option_type']} "
                f"expired unsold; realized {realized:.2f}",
                user_id=pos['user_id']
            )

        # ---------------------------------------------------------
        # 1. PROCESS LIMIT SELL ORDERS
        # ---------------------------------------------------------
        cursor.execute("""
            SELECT l.id as limit_id, l.target_price, l.sell_order_id, l.user_id,
                   p.id as position_id, p.ticker, p.expiry, p.strike, p.option_type, p.total_quantity, p.average_cost
            FROM limit_orders l
            JOIN positions p ON l.position_id = p.id
            WHERE l.status = 'pending'
        """)
        sell_orders = cursor.fetchall()

        for order in sell_orders:
            user_id = order['user_id']
            config = get_user_config(user_id)
            paper_mode = config.execution.get('paper_mode', True)
            cutoff_time_str = config.execution.get('zero_dte_market_sell_cutoff', '15:50')
            cutoff_time = datetime.strptime(cutoff_time_str, "%H:%M").time()

            is_0dte = (order['expiry'] == today_str)

            # Check 0DTE Cutoff rule
            if is_0dte and current_time >= cutoff_time:
                # Force Market Sell
                quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'], order['average_cost'])
                fill_price = quote['bid'] # Market sell hits the bid

                pnl_dollars = round((fill_price - order['average_cost']) * order['total_quantity'] * 100, 2)
                pnl_pct = round(((fill_price / order['average_cost']) - 1) * 100, 2)

                cursor.execute("""
                    UPDATE limit_orders
                    SET status = 'filled', fill_timestamp = datetime('now', 'localtime'), realized_pnl = ?
                    WHERE id = ?
                """, (pnl_dollars, order['limit_id']))

                _close_position(cursor, order['position_id'])

                notifications.append(lambda o=order, f=fill_price, d=pnl_dollars, p=pnl_pct: notify_limit_sell_filled(
                    o['user_id'], o['total_quantity'], o['ticker'], o['strike'], o['option_type'], f, d, p))
                continue

            # Check for limit fill (Paper Mode Simulation)
            if paper_mode:
                quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'], order['average_cost'])
                if quote['bid'] >= order['target_price']:
                    # Simulate Fill
                    pnl_dollars = round((order['target_price'] - order['average_cost']) * order['total_quantity'] * 100, 2)
                    pnl_pct = round(((order['target_price'] / order['average_cost']) - 1) * 100, 2)

                    cursor.execute("""
                        UPDATE limit_orders
                        SET status = 'filled', fill_timestamp = datetime('now', 'localtime'), realized_pnl = ?
                        WHERE id = ?
                    """, (pnl_dollars, order['limit_id']))

                    _close_position(cursor, order['position_id'])

                    notifications.append(lambda o=order, d=pnl_dollars, p=pnl_pct: notify_limit_sell_filled(
                        o['user_id'], o['total_quantity'], o['ticker'], o['strike'], o['option_type'], o['target_price'], d, p))
            else:
                # Live Mode: Poll Robinhood MCP for order status
                pass

        # ---------------------------------------------------------
        # 2. PROCESS LIMIT BUY ORDERS
        # ---------------------------------------------------------
        cursor.execute("""
            SELECT b.id as buy_id, b.decision_id, b.buy_order_id, b.target_price, b.quantity,
                   b.created_at, b.user_id,
                   a.ticker, a.expiry, a.strike, a.option_type, a.action as signal_action
            FROM limit_buy_orders b
            JOIN alerts a ON b.alert_id = a.id
            WHERE b.status = 'pending'
        """)
        buy_orders = cursor.fetchall()

        for order in buy_orders:
            user_id = order['user_id']
            config = get_user_config(user_id)
            paper_mode = config.execution.get('paper_mode', True)
            cutoff_time_str = config.execution.get('zero_dte_market_sell_cutoff', '15:50')
            cutoff_time = datetime.strptime(cutoff_time_str, "%H:%M").time()

            is_0dte = (order['expiry'] == today_str)

            # Check 0DTE Cutoff rule - cancel buys at end of day
            if is_0dte and current_time >= cutoff_time:
                cursor.execute("UPDATE limit_buy_orders SET status = 'cancelled' WHERE id = ?", (order['buy_id'],))
                _log_event(cursor, 'system', f"Cancelled stale 0DTE limit buy order #{order['buy_id']} for {order['ticker']}", user_id=user_id)
                continue

            # Cancel stale non-0DTE limit buy orders older than 24 hours
            if order['created_at']:
                # created_at is written with SQLite's 'localtime'. Interpret it in
                # the configured market timezone rather than the VPS clock, which
                # spec 2.1 warns may be UTC.
                created_dt = NY_TZ.localize(datetime.strptime(order['created_at'], "%Y-%m-%d %H:%M:%S"))
                age_hours = (now - created_dt).total_seconds() / 3600
                if age_hours > 24:
                    cursor.execute("UPDATE limit_buy_orders SET status = 'cancelled' WHERE id = ?", (order['buy_id'],))
                    _log_event(cursor, 'system', f"Cancelled stale limit buy order #{order['buy_id']} for {order['ticker']} (age: {age_hours:.1f}h)", user_id=user_id)
                    continue

            if paper_mode:
                quote = get_live_quote(order['ticker'], order['expiry'], order['strike'], order['option_type'], order['target_price'])
                # Buy order fills if ask drops to target price
                if quote['ask'] <= order['target_price']:
                    # Import here to avoid circular imports if any
                    from src.services.executor import process_buy_fill

                    # We fill at the target price (limit price)
                    fill_price = order['target_price']

                    # Execute the position update / take-profit logic on OUR
                    # connection, so the fill and the 'filled' stamp below commit
                    # together - otherwise a failure between them leaves the order
                    # pending and the next pass buys the position a second time.
                    notifications.extend(process_buy_fill(
                        user_id=user_id,
                        decision_id=order['decision_id'],
                        ticker=order['ticker'],
                        expiry=order['expiry'],
                        strike=order['strike'],
                        option_type=order['option_type'],
                        signal_action=order['signal_action'],
                        fill_price=fill_price,
                        contracts=order['quantity'],
                        paper_mode=paper_mode,
                        order_id=order['buy_order_id'],
                        conn=conn
                    ))

                    # Mark as filled
                    cursor.execute("""
                        UPDATE limit_buy_orders
                        SET status = 'filled', fill_timestamp = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (order['buy_id'],))
            else:
                # Live Mode: Poll Robinhood MCP for order status
                pass

        conn.commit()
    finally:
        conn.close()

    run_notifications(notifications)
