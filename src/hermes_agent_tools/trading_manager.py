"""
Nous Hermes Agent Custom Tools

This module provides the necessary functions to allow the Nous Hermes Agent 
to act as the "Manager" for the high-frequency trading bot.

To use these in the Hermes Agent framework, you can register these functions 
as tools (e.g., using @tool decorators depending on the underlying framework like 
LangChain or LlamaIndex used by Hermes Agent).
"""
import os
import sqlite3
import json

# Adjust this path based on where the Hermes Agent is running relative to this project
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'hermes.db'))
HALT_FILE = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'HALT'))

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def get_open_positions() -> str:
    """
    Tool: Returns a JSON string of all currently open options positions held by the trading bot.
    Use this when the user asks "What positions are currently open?" or "What are we holding?"
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = dict_factory
        cur = conn.cursor()
        cur.execute("SELECT ticker, expiry, strike, option_type, total_quantity, average_cost FROM positions WHERE status = 'open'")
        positions = cur.fetchall()
        conn.close()
        
        if not positions:
            return "There are currently no open positions."
        return json.dumps(positions, indent=2)
    except Exception as e:
        return f"Error retrieving positions: {e}"

def get_todays_realized_pnl() -> str:
    """
    Tool: Returns the total realized profit or loss (in dollars) for today's trading activity.
    Use this when the user asks "How much money did we make today?" or "What is today's PnL?"
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT SUM(realized_pnl) FROM limit_orders WHERE status = 'filled' AND date(fill_timestamp) = date('now', 'localtime')")
        pnl = cur.fetchone()[0]
        conn.close()
        
        if pnl is None:
            return "No trades have been closed today. Realized P/L is $0.00."
        return f"Today's total realized P/L is ${pnl:.2f}."
    except Exception as e:
        return f"Error calculating P/L: {e}"

def trigger_kill_switch() -> str:
    """
    Tool: Instantly halts all trading activity by triggering the global kill switch.
    Use this when the user asks to "stop trading", "pause the bot", "halt", or "emergency stop".
    """
    try:
        with open(HALT_FILE, 'w') as f:
            f.write("HALTED BY HERMES AGENT")
        return "SUCCESS: Global kill switch engaged. All trading has been halted."
    except Exception as e:
        return f"ERROR: Failed to engage kill switch: {e}"

def resume_trading() -> str:
    """
    Tool: Resumes trading activity by removing the global kill switch.
    Use this when the user asks to "resume trading", "start the bot", or "unpause".
    """
    try:
        if os.path.exists(HALT_FILE):
            os.remove(HALT_FILE)
            return "SUCCESS: Kill switch removed. Trading operations have resumed."
        else:
            return "Trading is already active. The kill switch was not engaged."
    except Exception as e:
        return f"ERROR: Failed to resume trading: {e}"

def get_system_status() -> str:
    """
    Tool: Returns the current health and status of the trading bot, including API quotas.
    Use this when the user asks "Is the bot running?" or "What is the system status?"
    """
    try:
        is_halted = os.path.exists(HALT_FILE)
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM api_calls WHERE service = 'x' AND strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')")
        api_count = cur.fetchone()[0]
        conn.close()
        
        status = "HALTED" if is_halted else "ACTIVE"
        
        return f"Trading System is {status}. Twitter API calls used this month: {api_count}."
    except Exception as e:
        return f"Error retrieving system status: {e}"
