import os
import sys
import time
import secrets
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import check_password_hash
from datetime import datetime
import pytz
import json

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.core.db import get_connection, log_system_event
from src.core.config import get_user_config, system_config
from src.services.notifier import notify_mode_change
from src.services.quotes import provider_meta
from src.services.broker_limits import current_usage as broker_usage

app = Flask(__name__)
def _resolve_secret_key():
    """Session signing key, in order of preference: config, environment, then a
    generated key persisted in the database.

    The persisted fallback matters operationally: a fresh random key per process
    logs every user out on each restart, which trains people to ignore it. Note
    it lives in the same database as the password hashes, so it offers no
    protection against someone who already has read access to that file.
    """
    configured = system_config.dashboard.get("secret_key") or os.environ.get("DASHBOARD_SECRET_KEY")
    if configured:
        return configured

    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # The dashboard does not run init_db(), and systemd gives no ordering
            # guarantee against rb-mcp on boot. Create the table if we got here
            # first, so the key persists rather than silently going random.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at DATETIME DEFAULT (datetime('now', 'localtime'))
                )
            """)
            row = cursor.execute(
                "SELECT value FROM system_state WHERE key = 'dashboard_secret_key'"
            ).fetchone()
            if row and row['value']:
                return row['value']

            key = secrets.token_hex(32)
            cursor.execute("""
                INSERT INTO system_state (key, value) VALUES ('dashboard_secret_key', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key,))
            conn.commit()
            return key
        finally:
            conn.close()
    except Exception as e:
        # Database not initialised yet (rb-mcp runs init_db, not the dashboard).
        print(f"WARNING: could not persist dashboard secret key ({e}); "
              "sessions will not survive a restart.")
        return secrets.token_hex(32)


app.secret_key = _resolve_secret_key()

# The dashboard often starts before the trading service, and it is where the
# "Needs login" status is read from - so it also adopts a leftover pre-per-user
# token rather than reporting a login that exists as missing.
try:
    from src.services.robinhood_auth import migrate_legacy_token
    migrate_legacy_token()
except Exception as e:
    print(f"WARNING: Robinhood token migration check failed: {e}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

NY_TZ = pytz.timezone(system_config.polling.get("timezone", "America/New_York"))

# Decision knobs the read-only dashboard is allowed to tune (spec 7.3).
# paper_mode and all execution settings are intentionally excluded.
#
# Raising these increases exposure, so they are capped at the config.yaml value
# and the UI can only ever tighten them.
RISK_CEILING_KEYS = (
    "contracts_per_signal",
    "price_tolerance_pct",
    "max_daily_spend_usd",
    "max_open_positions",
    "per_trade_max_spend_usd",
    "max_spend_per_position_usd",
    "risk_per_signal_usd",
)
# Raising these does not increase capital at risk (a higher profit target or a
# deeper limit-buy discount is the more conservative choice), so they are only
# range-checked, not capped at the system default.
FREE_PCT_KEYS = ("take_profit_pct", "limit_buy_discount_pct")
# Lowering these tightens risk, so they are range-checked but not capped:
# a shorter staleness window and a smaller loss cap are both safer.
FREE_NUMERIC_KEYS = ("max_signal_age_sec", "max_daily_loss_usd")

SETTABLE_DECISION_KEYS = RISK_CEILING_KEYS + FREE_PCT_KEYS + FREE_NUMERIC_KEYS
INTEGER_DECISION_KEYS = ("contracts_per_signal", "max_open_positions", "max_signal_age_sec")

# Non-numeric settings: a soft pause, style/ticker filters, and sizing mode.
# None of these can raise exposure, so they need validation but no ceiling.
BOOL_DECISION_KEYS = ("trading_enabled",)
LIST_DECISION_KEYS = ("skip_trade_styles", "allowed_tickers", "blocked_tickers")
KNOWN_TRADE_STYLES = ("0DTE", "SWING", "DAYTRADE", "LOTTO")
SIZING_MODES = ("contracts", "dollars")

HERMES_AGENT_CONFIG = os.path.expanduser("~/.hermes/config.yaml")


def agent_mcp_registered():
    """Whether the Hermes Agent has an enabled `robinhood` MCP server.

    Best-effort and read-only: the agent owns this file, we only report on it.
    Registration with the agent does not mean this process can place orders -
    the agent holds the OAuth session, not us.
    """
    try:
        import yaml
        with open(HERMES_AGENT_CONFIG) as handle:
            agent_config = yaml.safe_load(handle) or {}
        server = (agent_config.get('mcp_servers') or {}).get('robinhood') or {}
        return bool(server.get('enabled', False))
    except Exception:
        return False


def robinhood_status(user_config, user_id):
    """Honest report of Robinhood MCP readiness.

    Deliberately driven by whether the quote provider is actually implemented,
    not by whether someone selected it in config. Showing 'connected' because a
    config key says 'robinhood' would be worse than showing nothing - it would
    invite the assumption that live trading works.
    """
    source = user_config.execution.get('quote_source', 'simulated')
    meta = provider_meta('robinhood')

    account_row = query_db(
        "SELECT robinhood_account_id FROM users WHERE id = ?", (user_id,), one=True
    )
    account_id_set = bool(account_row and account_row.get('robinhood_account_id'))
    registered = agent_mcp_registered()

    # Two processes, two different levels of access. The Hermes Agent may hold a
    # perfectly good OAuth session and answer "what are my positions?" while this
    # trading process still cannot place an order - it is a separate process with
    # no MCP client and no share of that session. Reporting a flat "not
    # integrated" when the agent visibly works reads as a bug, so name the state.
    try:
        from src.services.robinhood_auth import have_credentials
        logged_in = have_credentials(user_id)
    except Exception:
        logged_in = False

    if meta['implemented'] and not logged_in:
        # The code exists but cannot authenticate, so it would fail closed on
        # every quote. Say so rather than implying it is ready.
        status, label = 'needs_login', 'Needs login'
    elif meta['implemented'] and source == 'robinhood':
        status, label = 'active', 'Active'
    elif meta['implemented']:
        status, label = 'available', 'Available (not selected)'
    elif registered:
        status, label = 'agent_only', 'Agent only - no trade routing'
    else:
        status, label = 'not_configured', 'Not configured'

    calls_last_min, refused = broker_usage()
    budget = system_config.settings.get('execution', {}).get('broker_rate_limit_per_min', 60)

    prerequisites = [
        f"Bot logged in: {'yes' if logged_in else 'no'}",
        f"Account ID set: {'yes' if account_id_set else 'no'}",
        f"Quote source: {source}",
        f"API budget: {calls_last_min}/{budget} per min",
    ]
    if status == 'needs_login':
        prerequisites.insert(0, "Run scripts/robinhood_login.py")
    if refused:
        prerequisites.append(f"Refused (rate limit): {refused}")
    if status == 'agent_only':
        detail = ("Your Hermes Agent can query Robinhood (positions, balances) - "
                  "that part works. The trading process is separate and cannot place "
                  "orders: it has no MCP client and does not share the agent's session. "
                  + " | ".join(prerequisites))
    elif status == 'not_configured':
        detail = ("Robinhood is not set up. Register the MCP server with the Hermes "
                  "Agent, then wire a provider for the trading process. "
                  + " | ".join(prerequisites))
    else:
        detail = " | ".join(prerequisites)

    return {
        "status": status,
        "label": label,
        "detail": detail,
        "quote_source": source,
        "agent_mcp_registered": registered,
        "account_id_set": account_id_set,
        "calls_last_min": calls_last_min,
        "rate_limit_per_min": budget,
        "calls_refused": refused,
    }


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

# Login throttling. In-process and per-IP: enough to stop credential stuffing
# from a compromised device on the tailnet, and it puts failed attempts in the
# dashboard's own event feed where they are visible.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SEC = 300
_failed_logins = {}

def _is_locked_out(source_ip):
    attempts = [t for t in _failed_logins.get(source_ip, []) if time.time() - t < LOGIN_LOCKOUT_SEC]
    _failed_logins[source_ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS

def _record_failed_login(source_ip, username):
    _failed_logins.setdefault(source_ip, []).append(time.time())
    attempts = len(_failed_logins[source_ip])
    log_system_event('auth_failure', f"Failed login for '{username}' from {source_ip} (attempt {attempts})")
    if attempts >= LOGIN_MAX_ATTEMPTS:
        log_system_event('auth_lockout', f"Locked out {source_ip} after {attempts} failed logins")

class User(UserMixin):
    def __init__(self, id, username, is_admin, active_status):
        self.id = str(id)
        self.username = username
        self.is_admin = is_admin
        self.active_status = active_status

    @property
    def is_active(self):
        return bool(self.active_status)

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
        source_ip = request.remote_addr or 'unknown'

        if _is_locked_out(source_ip):
            flash("Too many failed attempts. Try again in a few minutes.")
            return render_template('login.html')

        user_data = query_db("SELECT * FROM users WHERE username = ?", (username,), one=True)
        if user_data and check_password_hash(user_data['password_hash'], password):
            if not user_data['is_active']:
                flash("Account is inactive.")
                return redirect(url_for('login'))
            _failed_logins.pop(source_ip, None)
            user = User(user_data['id'], user_data['username'], user_data['is_admin'], user_data['is_active'])
            login_user(user)
            return redirect(url_for('index'))
        else:
            _record_failed_login(source_ip, username)
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

    trading_halt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT_TRADING")
    entries_paused = os.path.exists(trading_halt_path) or not user_config.decision.get("trading_enabled", True)

    heartbeat = query_db("SELECT value FROM system_state WHERE key = 'poller_last_run'", one=True)
    poller_last_run = heartbeat['value'] if heartbeat else None
    poller_age_sec = None
    if poller_last_run:
        try:
            last = NY_TZ.localize(datetime.strptime(poller_last_run, "%Y-%m-%d %H:%M:%S"))
            poller_age_sec = round((datetime.now(NY_TZ) - last).total_seconds())
        except (ValueError, TypeError):
            poller_age_sec = None

    if kill_switch_active:
        status = "halted"
    elif entries_paused:
        status = "paused"
    else:
        status = "active"

    return jsonify({
        "status": status,
        "paper_mode": user_config.execution.get("paper_mode", True),
        "quote_source": user_config.execution.get("quote_source", "simulated"),
        "api_quota_used": count,
        "api_quota_limit": limit,
        "quota_pct": round((count / limit) * 100, 2) if limit else 0,
        "poller_last_run": poller_last_run,
        "poller_age_sec": poller_age_sec,
        "robinhood": robinhood_status(user_config, current_user.id)
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
               d.action_taken, d.observed_price, d.reasoning, d.latency_sec,
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
        stored = json.loads(row['config_json']) if row else {}
        # Surface the *effective* paper_mode, so the toggle reflects reality even
        # when the user has no stored override and the value comes from config.yaml.
        effective = get_user_config(current_user.id)
        stored.setdefault('execution', {})['paper_mode'] = effective.execution.get('paper_mode', True)
        return jsonify(stored)
    else:
        # Spec 7.3: the dashboard must not become a casual path to live trading.
        # The web UI may only tune the decision knobs below, and only in the safe
        # direction - risk limits are clamped to the system ceilings in
        # config.yaml, never raised above them.
        #
        # paper_mode IS settable, but only behind a typed confirmation, and every
        # change is written to system_events and pushed to Discord/WhatsApp. The
        # goal is that enabling live trading can never be a stray click, and can
        # never happen without you hearing about it.
        incoming = request.json or {}
        incoming_decision = incoming.get('decision') or {}
        system_decision = system_config.settings.get('decision', {})

        clamped = {}
        errors = []
        for key in SETTABLE_DECISION_KEYS:
            if key not in incoming_decision:
                continue
            try:
                value = float(incoming_decision[key])
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
            if value <= 0:
                errors.append(f"{key} must be greater than zero")
                continue
            if key in FREE_PCT_KEYS:
                if value > 100:
                    errors.append(f"{key} must be between 0 and 100")
                    continue
            elif key in FREE_NUMERIC_KEYS:
                pass  # lowering these tightens risk; no ceiling needed
            else:
                ceiling = system_decision.get(key)
                if ceiling is not None and value > ceiling:
                    value = ceiling
            clamped[key] = int(value) if key in INTEGER_DECISION_KEYS else value

        for key in BOOL_DECISION_KEYS:
            if key in incoming_decision:
                clamped[key] = bool(incoming_decision[key])

        if 'sizing_mode' in incoming_decision:
            mode = str(incoming_decision['sizing_mode'])
            if mode not in SIZING_MODES:
                errors.append(f"sizing_mode must be one of {', '.join(SIZING_MODES)}")
            else:
                clamped['sizing_mode'] = mode

        for key in LIST_DECISION_KEYS:
            if key not in incoming_decision:
                continue
            raw = incoming_decision[key]
            if not isinstance(raw, list):
                errors.append(f"{key} must be a list")
                continue
            values = [str(v).strip().upper() for v in raw if str(v).strip()]
            if key == 'skip_trade_styles':
                unknown = [v for v in values if v not in KNOWN_TRADE_STYLES]
                if unknown:
                    errors.append(f"unknown trade styles: {', '.join(unknown)}")
                    continue
            clamped[key] = values

        # Paper/live mode. Only a genuine change is acted on, and switching *to*
        # live requires typing LIVE. Switching back to paper is the safe
        # direction and needs no confirmation.
        incoming_execution = incoming.get('execution') or {}
        mode_change_to_paper = None
        if 'paper_mode' in incoming_execution:
            requested_paper = bool(incoming_execution['paper_mode'])
            current_paper = get_user_config(current_user.id).execution.get('paper_mode', True)
            if requested_paper != current_paper:
                confirmation = str(incoming.get('confirm') or '').strip().upper()
                if not requested_paper and confirmation != 'LIVE':
                    errors.append("Switching to live trading requires typing LIVE to confirm")
                else:
                    mode_change_to_paper = requested_paper

        if errors:
            return jsonify({"status": "error", "errors": errors}), 400

        row = query_db("SELECT config_json FROM user_configs WHERE user_id = ?", (current_user.id,), one=True)
        stored = json.loads(row['config_json']) if row else {}
        stored.setdefault('decision', {}).update(clamped)
        if mode_change_to_paper is not None:
            stored.setdefault('execution', {})['paper_mode'] = mode_change_to_paper

        query_db("""
            INSERT INTO user_configs (user_id, config_json)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET config_json=excluded.config_json, updated_at=datetime('now', 'localtime')
        """, (current_user.id, json.dumps(stored)))

        # Audit and announce a mode change only after it is durably stored.
        if mode_change_to_paper is not None:
            going_live = not mode_change_to_paper
            detail = (f"Trading mode set to {'PAPER' if mode_change_to_paper else 'LIVE'} "
                      f"by {current_user.username} from {request.remote_addr}")
            log_system_event('mode_change', detail, user_id=int(current_user.id))
            try:
                notify_mode_change(int(current_user.id), going_live, current_user.username)
            except Exception as e:
                print(f"Failed to send mode-change notification: {e}")

        return jsonify({
            "status": "success",
            "applied": clamped,
            "paper_mode": mode_change_to_paper if mode_change_to_paper is not None else None
        })

if __name__ == '__main__':
    bind = system_config.dashboard.get("bind_address", "127.0.0.1")
    port = system_config.dashboard.get("port", 8420)
    app.run(host=bind, port=port, debug=False)
