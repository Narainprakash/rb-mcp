import os
import sys
from flask import Flask, jsonify, render_template
from datetime import datetime
import pytz

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.core.db import get_connection
from src.core.config import config

app = Flask(__name__)
NY_TZ = pytz.timezone(config.polling.get("timezone", "America/New_York"))

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def query_db(query, args=(), one=False):
    conn = get_connection()
    conn.row_factory = dict_factory
    cur = conn.cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/health')
def health():
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    kill_switch_active = os.path.exists(halt_file_path)
    
    # Check quota
    now = datetime.now(NY_TZ)
    start_of_month = f"{now.year}-{now.month:02d}-01 00:00:00"
    api_calls = query_db("SELECT COUNT(*) as count FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,), one=True)
    count = api_calls['count'] if api_calls else 0
    limit = config.polling.get("monthly_api_call_ceiling", 10000)
    
    return jsonify({
        "status": "halted" if kill_switch_active else "active",
        "paper_mode": config.execution.get("paper_mode", True),
        "api_quota_used": count,
        "api_quota_limit": limit,
        "quota_pct": round((count / limit) * 100, 2)
    })

@app.route('/api/stats')
def stats():
    now = datetime.now(NY_TZ)
    start_of_day = f"{now.year}-{now.month:02d}-{now.day:02d} 00:00:00"
    
    data = query_db("""
        SELECT 
            SUM(realized_pnl) as total_pnl,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses
        FROM limit_orders 
        WHERE status = 'filled' AND fill_timestamp >= ?
    """, (start_of_day,), one=True)
    
    car_data = query_db("""
        SELECT SUM(average_cost * total_quantity * 100) as car 
        FROM positions 
        WHERE status = 'open'
    """, one=True)
    car = car_data['car'] if car_data and car_data['car'] is not None else 0.0
    
    return jsonify({
        "daily_pnl": data['total_pnl'] if data and data['total_pnl'] is not None else 0.0,
        "wins": data['wins'] if data and data['wins'] is not None else 0,
        "losses": data['losses'] if data and data['losses'] is not None else 0,
        "capital_at_risk": car
    })

@app.route('/api/feed')
def feed():
    # Returns last 50 alerts, joined with decisions and trades
    data = query_db("""
        SELECT a.id, a.timestamp as alert_time, a.action, a.ticker, a.expiry, a.strike, a.option_type, a.price as rec_price,
               d.action_taken, d.observed_price, d.reasoning,
               t.status as trade_status, t.paper_mode, a.raw_text, a.parse_status
        FROM alerts a
        LEFT JOIN decisions d ON a.id = d.alert_id
        LEFT JOIN trades t ON d.id = t.decision_id
        ORDER BY a.id DESC LIMIT 50
    """)
    return jsonify(data)

@app.route('/api/positions')
def positions():
    data = query_db("""
        SELECT p.*, l.target_price, l.status as limit_status 
        FROM positions p
        LEFT JOIN limit_orders l ON p.id = l.position_id AND l.status = 'pending'
        WHERE p.status = 'open'
    """)
    return jsonify(data)

@app.route('/api/history')
def history():
    # Last 50 closed limit orders
    data = query_db("""
        SELECT l.*, p.ticker, p.expiry, p.strike, p.option_type, p.total_quantity, p.average_cost 
        FROM limit_orders l
        JOIN positions p ON l.position_id = p.id
        WHERE l.status = 'filled'
        ORDER BY l.fill_timestamp DESC LIMIT 50
    """)
    return jsonify(data)

@app.route('/api/limit_buys')
def limit_buys():
    data = query_db("""
        SELECT l.*, a.ticker, a.expiry, a.strike, a.option_type 
        FROM limit_buy_orders l
        JOIN alerts a ON l.alert_id = a.id
        WHERE l.status = 'pending'
    """)
    return jsonify(data)

@app.route('/api/events')
def events():
    data = query_db("""
        SELECT * FROM system_events 
        ORDER BY id DESC LIMIT 50
    """)
    return jsonify(data)

@app.route('/api/parse_failures')
def parse_failures():
    data = query_db("""
        SELECT * FROM alerts 
        WHERE parse_status = 'needs_review' 
        ORDER BY id DESC LIMIT 20
    """)
    return jsonify(data)

if __name__ == '__main__':
    bind = config.dashboard.get("bind_address", "127.0.0.1")
    port = config.dashboard.get("port", 8420)
    app.run(host=bind, port=port, debug=True)
