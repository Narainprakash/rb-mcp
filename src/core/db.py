import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hermes.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT UNIQUE NOT NULL,
    raw_text TEXT NOT NULL,
    action TEXT,
    ticker TEXT,
    expiry TEXT,
    strike REAL,
    option_type TEXT,
    price REAL,
    trade_style TEXT,
    parse_status TEXT NOT NULL, -- 'success' or 'needs_review'
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    recommended_price REAL NOT NULL,
    observed_price REAL,
    action_taken TEXT NOT NULL, -- 'buy', 'skip', 'error'
    reasoning TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(alert_id) REFERENCES alerts(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    paper_mode BOOLEAN NOT NULL,
    buy_order_id TEXT,
    fill_price REAL,
    quantity INTEGER,
    status TEXT NOT NULL, -- 'open', 'closed', 'expired'
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    total_quantity INTEGER DEFAULT 0,
    average_cost REAL DEFAULT 0.0,
    status TEXT NOT NULL, -- 'open', 'closed'
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS limit_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    sell_order_id TEXT,
    target_price REAL NOT NULL,
    status TEXT NOT NULL, -- 'pending', 'filled', 'cancelled'
    fill_timestamp DATETIME,
    realized_pnl REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS limit_buy_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    alert_id INTEGER NOT NULL,
    buy_order_id TEXT,
    target_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL, -- 'pending', 'filled', 'cancelled'
    fill_timestamp DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(decision_id) REFERENCES decisions(id),
    FOREIGN KEY(alert_id) REFERENCES alerts(id)
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL, -- 'x' or 'robinhood'
    endpoint TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executescript(SCHEMA)
    conn.commit()
    conn.close()

def log_system_event(event_type: str, message: str):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO system_events (event_type, message) VALUES (?, ?)", (event_type, message))
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
