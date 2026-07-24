import os
import sys
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import check_password_hash
from datetime import datetime
import pytz
import json

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.core.db import get_connection
from src.core.config import get_user_config, system_config

app = Flask(__name__)
app.secret_key = system_config.dashboard.get("secret_key", "super_secret_dev_key")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

NY_TZ = pytz.timezone(system_config.polling.get("timezone", "America/New_York"))

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
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv

class User(UserMixin):
    def __init__(self, id, username, is_admin, is_active):
        self.id = id
        self.username = username
        self.is_admin = is_admin
        self.is_active = is_active

@login_manager.user_loader
def load_user(user_id):
    user_data = query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user_data:
        return User(user_data['id'], user_data['username'], user_data['is_admin'], user_data['is_active'])
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user_data = query_db("SELECT * FROM users WHERE username = ?", (username,), one=True)
        if user_data and check_password_hash(user_data['password_hash'], password):
            if not user_data['is_active']:
                flash("Account is inactive.")
                return redirect(url_for('login'))
            user = User(user_data['id'], user_data['username'], user_data['is_admin'], user_data['is_active'])
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash("Invalid username or password.")
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html', username=current_user.username)

@app.route('/api/health')
@login_required
def health():
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    kill_switch_active = os.path.exists(halt_file_path)
    
    # Check global quota
    now = datetime.now(NY_TZ)
    start_of_month = f"{now.year}-{now.month:02d}-01 00:00:00"
    api_calls = query_db("SELECT COUNT(*) as count FROM api_calls WHERE service = 'x' AND timestamp >= ?", (start_of_month,), one=True)
    count = api_calls['count'] if api_calls else 0
    limit = system_config.polling.get("monthly_api_call_ceiling", 10000)
    
    user_config = get_user_config(current_user.id)
    
    return jsonify({
        "status": "halted" if kill_switch_active else "active",
        "paper_mode": user_config.execution.get("paper_mode", True),
        "api_quota_used": count,
        "api_quota_limit": limit,
        "quota_pct": round((count / limit) * 100, 2)
    })

@app.route('/api/stats')
@login_required
def stats():
    now = datetime.now(NY_TZ)
    start_of_day = f"{now.year}-{now.month:02d}-{now.day:02d} 00:00:00"
    
    data = query_db("""
        SELECT 
            SUM(realized_pnl) as total_pnl,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses
        FROM limit_orders 
        WHERE user_id = ? AND status = 'filled' AND fill_timestamp >= ?
    """, (current_user.id, start_of_day), one=True)
    
    car_data = query_db("""
        SELECT SUM(average_cost * total_quantity * 100) as car 
        FROM positions 
        WHERE user_id = ? AND status = 'open'
    """, (current_user.id,), one=True)
    car = car_data['car'] if car_data and car_data['car'] is not None else 0.0
    
    return jsonify({
        "daily_pnl": data['total_pnl'] if data and data['total_pnl'] is not None else 0.0,
        "wins": data['wins'] if data and data['wins'] is not None else 0,
        "losses": data['losses'] if data and data['losses'] is not None else 0,
        "capital_at_risk": car
    })

@app.route('/api/feed')
@login_required
def feed():
    # Returns last 50 alerts, joined with decisions and trades for the current user
    data = query_db("""
        SELECT a.id, a.timestamp as alert_time, a.action, a.ticker, a.expiry, a.strike, a.option_type, a.price as rec_price,
               d.action_taken, d.observed_price, d.reasoning,
               t.status as trade_status, t.paper_mode, a.raw_text, a.parse_status
        FROM alerts a
        LEFT JOIN decisions d ON a.id = d.alert_id AND d.user_id = ?
        LEFT JOIN trades t ON d.id = t.decision_id AND t.user_id = ?
        ORDER BY a.id DESC LIMIT 50
    """, (current_user.id, current_user.id))
    return jsonify(data)

@app.route('/api/positions')
@login_required
def positions():
    data = query_db("""
        SELECT p.*, l.target_price, l.status as limit_status 
        FROM positions p
        LEFT JOIN limit_orders l ON p.id = l.position_id AND l.status = 'pending'
        WHERE p.user_id = ? AND p.status = 'open'
    """, (current_user.id,))
    return jsonify(data)

@app.route('/api/history')
@login_required
def history():
    # Last 50 closed limit orders
    data = query_db("""
        SELECT l.*, p.ticker, p.expiry, p.strike, p.option_type, p.total_quantity, p.average_cost 
        FROM limit_orders l
        JOIN positions p ON l.position_id = p.id
        WHERE l.user_id = ? AND l.status = 'filled'
        ORDER BY l.fill_timestamp DESC LIMIT 50
    """, (current_user.id,))
    return jsonify(data)

@app.route('/api/limit_buys')
@login_required
def limit_buys():
    data = query_db("""
        SELECT l.*, a.ticker, a.expiry, a.strike, a.option_type 
        FROM limit_buy_orders l
        JOIN alerts a ON l.alert_id = a.id
        WHERE l.user_id = ? AND l.status = 'pending'
    """, (current_user.id,))
    return jsonify(data)

@app.route('/api/events')
@login_required
def events():
    # Global system events or events for this user
    data = query_db("""
        SELECT * FROM system_events 
        WHERE user_id = ? OR user_id IS NULL
        ORDER BY id DESC LIMIT 50
    """, (current_user.id,))
    return jsonify(data)

@app.route('/api/parse_failures')
@login_required
def parse_failures():
    # Parse failures are global alerts
    data = query_db("""
        SELECT * FROM alerts 
        WHERE parse_status = 'needs_review' 
        ORDER BY id DESC LIMIT 20
    """)
    return jsonify(data)

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'GET':
        row = query_db("SELECT config_json FROM user_configs WHERE user_id = ?", (current_user.id,), one=True)
        if row:
            return jsonify(json.loads(row['config_json']))
        return jsonify({})
    else:
        new_config = request.json
        query_db("""
            INSERT INTO user_configs (user_id, config_json)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET config_json=excluded.config_json, updated_at=datetime('now', 'localtime')
        """, (current_user.id, json.dumps(new_config)))
        return jsonify({"status": "success"})

if __name__ == '__main__':
    bind = system_config.dashboard.get("bind_address", "127.0.0.1")
    port = system_config.dashboard.get("port", 8420)
    app.run(host=bind, port=port, debug=True)
