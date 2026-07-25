import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hermes_mt.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    robinhood_account_id TEXT,
    created_at DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS user_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL,
    config_json TEXT NOT NULL,
    updated_at DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

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
    timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    alert_id INTEGER NOT NULL,
    recommended_price REAL NOT NULL,
    observed_price REAL,
    action_taken TEXT NOT NULL, -- 'buy', 'skip', 'error'
    reasoning TEXT,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(alert_id) REFERENCES alerts(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    paper_mode BOOLEAN NOT NULL,
    buy_order_id TEXT,
    fill_price REAL,
    quantity INTEGER,
    status TEXT NOT NULL, -- 'open', 'closed', 'expired'
    timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    total_quantity INTEGER DEFAULT 0,
    average_cost REAL DEFAULT 0.0,
    status TEXT NOT NULL, -- 'open', 'closed', 'expired'
    add_without_parent BOOLEAN DEFAULT 0, -- ADD alert with no matching open position
    updated_at DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS limit_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    sell_order_id TEXT,
    target_price REAL NOT NULL,
    status TEXT NOT NULL, -- 'pending', 'filled', 'cancelled'
    fill_timestamp DATETIME,
    realized_pnl REAL,
    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS limit_buy_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    alert_id INTEGER NOT NULL,
    buy_order_id TEXT,
    target_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL, -- 'pending', 'filled', 'cancelled'
    fill_timestamp DATETIME,
    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(decision_id) REFERENCES decisions(id),
    FOREIGN KEY(alert_id) REFERENCES alerts(id)
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL, -- 'x' or 'robinhood'
    endpoint TEXT NOT NULL,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, -- Optional, NULL if system-wide
    event_type TEXT NOT NULL,
    message TEXT,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_alerts_parse_status ON alerts(parse_status);
CREATE INDEX IF NOT EXISTS idx_limit_orders_status ON limit_orders(status);
CREATE INDEX IF NOT EXISTS idx_limit_buy_orders_status ON limit_buy_orders(status);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_lookup ON positions(user_id, ticker, expiry, strike, option_type, status);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id);
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

    # Migrations for databases created before a column was added.
    cursor.execute("PRAGMA table_info(positions)")
    position_columns = {row['name'] for row in cursor.fetchall()}
    if 'add_without_parent' not in position_columns:
        cursor.execute("ALTER TABLE positions ADD COLUMN add_without_parent BOOLEAN DEFAULT 0")

    conn.commit()
    conn.close()

def log_system_event(event_type: str, message: str, user_id: int = None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO system_events (user_id, event_type, message) VALUES (?, ?, ?)", (user_id, event_type, message))
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
