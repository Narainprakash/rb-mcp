"""Regression tests for the defects found in the July 2026 code review."""
import json
import os
import re
import sqlite3
import subprocess
from datetime import date, timedelta

import pytest

from src.core.db import SCHEMA
from src.core.time_utils import get_ny_time
from src.services.executor import get_live_quote
from src.services.parser import parse_alert
from src.services.quotes import QuoteUnavailable, get_quote


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    # Match get_connection(): under WAL a reader can proceed while another
    # connection holds a write lock, which the buy-fill path relies on.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


# --- Parser: 0DTE expiry validation -----------------------------------------

def test_0dte_with_mismatched_expiry_is_flagged():
    """A 0DTE alert whose stated date isn't today must not be silently retargeted."""
    not_today = get_ny_time().date() + timedelta(days=30)
    tweet = f"#ALERT\nBTO $SPY {not_today.month}/{not_today.day} 750C\n1.81\n0DTE"
    signal = parse_alert(tweet)
    assert signal.parse_status == "needs_review"
    assert signal.expiry is None


def test_0dte_with_todays_expiry_parses():
    today = get_ny_time().date()
    tweet = f"#ALERT\nBTO $SPY {today.month}/{today.day} 750C\n1.81\n0DTE"
    signal = parse_alert(tweet)
    assert signal.parse_status == "success"
    assert signal.expiry == f"{today.year}-{today.month:02d}-{today.day:02d}"


def test_non_alert_tweet_is_ignored_not_review():
    """Ordinary tweets must not raise REVIEW NEEDED (spec 2.3)."""
    signal = parse_alert("Markets look choppy today, stay patient.")
    assert signal.parse_status == "ignored"


def test_malformed_calendar_date_does_not_crash():
    signal = parse_alert("#ALERT\nBTO $SPY 13/45 750C\n1.81")
    assert signal.parse_status == "needs_review"


def test_non_positive_price_is_rejected():
    """A zero price would raise in the quote call before a decision row exists,
    leaving the alert to retry every 2s forever."""
    assert parse_alert("#ALERT\nBTO $SPY 8/21 750C\n0\nSWING").parse_status == "needs_review"


# --- Notifications ----------------------------------------------------------

def test_losing_exit_renders_a_negative_sign(monkeypatch):
    import src.services.notifier as notifier
    sent = []
    monkeypatch.setattr(notifier, "send_discord_message", lambda uid, ev, m: sent.append(m))
    monkeypatch.setattr(notifier, "send_whatsapp_message", lambda *a, **k: None)
    notifier.notify_limit_sell_filled(1, 1, "SPY", 750, "C", 0.75, -25.00, -25.00)
    assert "(+-" not in sent[0]
    assert "-$25.00 (-25.00%)" in sent[0]


# --- Executor: simulated quotes ---------------------------------------------

def test_quote_is_anchored_to_reference_price():
    """Quotes must track the reference price, not a random 1.00-3.00 walk."""
    quote = get_live_quote("SPY", "2026-08-21", 750, "C", 10.00)
    assert 8.0 < quote["bid"] < 12.0
    assert quote["ask"] > quote["bid"]


def test_quote_is_deterministic():
    a = get_live_quote("SPY", "2026-08-21", 750, "C", 2.00)
    b = get_live_quote("SPY", "2026-08-21", 750, "C", 2.00)
    assert a == b


def test_quote_requires_reference_price():
    with pytest.raises(QuoteUnavailable):
        get_live_quote("SPY", "2026-08-21", 750, "C", None)


def test_unknown_quote_source_fails_closed():
    """A bad provider name must not silently fall back to invented prices."""
    with pytest.raises(QuoteUnavailable):
        get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="nonexistent")


def test_robinhood_provider_fails_closed():
    with pytest.raises(QuoteUnavailable):
        get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood")


# --- Multi-tenancy: new users must not backfill historical alerts ------------

PENDING_ALERTS_SQL = """
    SELECT a.id FROM alerts a
    LEFT JOIN decisions d ON a.id = d.alert_id AND d.user_id = ?
    WHERE a.parse_status = 'success' AND d.id IS NULL
      AND a.timestamp >= ? AND a.timestamp < ?
    ORDER BY a.timestamp ASC
"""


def test_new_user_does_not_backfill_old_alerts(db):
    db.execute("INSERT INTO users (username, password_hash) VALUES ('alice', 'h')")
    old = "2026-07-01 09:30:00"
    today = get_ny_time().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO alerts (tweet_id, raw_text, action, ticker, expiry, strike, option_type, price, parse_status, timestamp)"
        " VALUES ('1', 'x', 'BTO', 'SPY', '2026-08-21', 750, 'C', 1.81, 'success', ?)",
        (old,),
    )
    db.commit()

    start, end = f"{today} 00:00:00", f"{today} 23:59:59"
    rows = db.execute(PENDING_ALERTS_SQL, (1, start, end)).fetchall()
    assert rows == [], "a stale alert from a prior day must not be queued for execution"


def test_todays_alert_is_still_picked_up(db):
    db.execute("INSERT INTO users (username, password_hash) VALUES ('alice', 'h')")
    today = get_ny_time().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO alerts (tweet_id, raw_text, action, ticker, expiry, strike, option_type, price, parse_status, timestamp)"
        " VALUES ('2', 'x', 'BTO', 'SPY', '2026-08-21', 750, 'C', 1.81, 'success', ?)",
        (f"{today} 09:30:00",),
    )
    db.commit()

    start, end = f"{today} 00:00:00", f"{today} 23:59:59"
    rows = db.execute(PENDING_ALERTS_SQL, (1, start, end)).fetchall()
    assert len(rows) == 1


# --- Schema: expiry reconciliation and add_without_parent -------------------

# --- Decision engine: new risk controls ------------------------------------

@pytest.fixture
def user_db(tmp_path, monkeypatch):
    """A one-user database wired into the modules that read it."""
    import src.core.db as core_db
    import src.core.config as core_config

    path = str(tmp_path / "u.db")
    monkeypatch.setattr(core_db, "DB_PATH", path)
    core_db.init_db()
    conn = core_db.get_connection()
    conn.execute("INSERT INTO users (username, password_hash, is_active) VALUES ('a','h',1)")
    conn.commit()
    conn.close()

    def set_config(overrides):
        conn = core_db.get_connection()
        conn.execute(
            "INSERT INTO user_configs (user_id, config_json) VALUES (1, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET config_json=excluded.config_json",
            (json.dumps(overrides),),
        )
        conn.commit()
        conn.close()

    return set_config


def test_soft_pause_blocks_new_entries(user_db):
    from src.services.decision import compute_decision

    user_db({"decision": {"trading_enabled": False}})
    action, reason, contracts = compute_decision(1, 1, "SPY", "2030-01-01", 750, "C", 1.81, 1.81)
    assert action == "skip"
    assert contracts == 0
    assert "paused" in reason


def test_trade_style_filter_skips_excluded_style(user_db):
    from src.services.decision import compute_decision

    user_db({"decision": {"skip_trade_styles": ["0DTE"]}})
    action, reason, _ = compute_decision(1, 1, "SPY", "2030-01-01", 750, "C", 1.81, 1.81, "0DTE,LOTTO")
    assert action == "skip"
    assert "0DTE" in reason

    # A style that isn't excluded still trades.
    action, _, _ = compute_decision(1, 1, "SPY", "2030-01-01", 750, "C", 1.81, 1.81, "SWING")
    assert action != "skip"


def test_ticker_block_and_allow_lists(user_db):
    from src.services.decision import compute_decision

    user_db({"decision": {"blocked_tickers": ["TSLA"]}})
    action, reason, _ = compute_decision(1, 1, "TSLA", "2030-01-01", 750, "C", 1.81, 1.81)
    assert action == "skip" and "blocked" in reason

    user_db({"decision": {"allowed_tickers": ["SPY"]}})
    action, reason, _ = compute_decision(1, 1, "QQQ", "2030-01-01", 750, "C", 1.81, 1.81)
    assert action == "skip" and "allowed" in reason
    action, _, _ = compute_decision(1, 1, "SPY", "2030-01-01", 750, "C", 1.81, 1.81)
    assert action != "skip"


def test_dollar_sizing_normalises_exposure(user_db):
    from src.services.decision import resolve_contracts

    user_db({"decision": {"sizing_mode": "dollars", "risk_per_signal_usd": 500}})
    # $500 budget: 5 contracts of a $1.00 option, 1 of a $5.00 option.
    assert resolve_contracts(1, 1.00) == 5
    assert resolve_contracts(1, 5.00) == 1
    # Too expensive for even one contract.
    assert resolve_contracts(1, 6.00) == 0

    # Default mode is unchanged by this feature.
    user_db({"decision": {"contracts_per_signal": 3}})
    assert resolve_contracts(1, 1.00) == 3


def test_daily_loss_limit_pauses_entries(user_db):
    from src.services.decision import compute_decision
    import src.core.db as core_db

    user_db({"decision": {"max_daily_loss_usd": 100}})
    conn = core_db.get_connection()
    conn.execute(
        "INSERT INTO positions (user_id, ticker, expiry, strike, option_type, total_quantity, average_cost, status)"
        " VALUES (1,'SPY','2030-01-01',750,'C',1,1.0,'closed')"
    )
    conn.execute(
        "INSERT INTO limit_orders (user_id, position_id, target_price, status, realized_pnl, fill_timestamp)"
        " VALUES (1, 1, 1.2, 'filled', -150.0, datetime('now','localtime'))"
    )
    conn.commit()
    conn.close()

    action, reason, _ = compute_decision(1, 1, "SPY", "2030-01-01", 750, "C", 1.81, 1.81)
    assert action == "skip"
    assert "loss limit" in reason


def test_init_db_upgrades_a_pre_migration_database(tmp_path, monkeypatch):
    """Indexes on newly added columns must be created after the ALTER TABLEs.
    On an existing database CREATE TABLE IF NOT EXISTS is a no-op, so building
    the index first fails with `no such column`."""
    import src.core.db as core_db

    # Use the real pre-migration schema from git rather than a hand-written
    # stub: a stub that omits columns which always existed tests a database
    # that never shipped, and misses the migration path that actually runs.
    old_src = subprocess.run(
        ["git", "show", "727bc2b:src/core/db.py"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)),
    ).stdout
    match = re.search(r'SCHEMA = """(.*?)"""', old_src, re.S)
    if not match:
        pytest.skip("historical schema unavailable (shallow clone?)")

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(match.group(1))
    old.execute(
        "INSERT INTO positions (user_id, ticker, expiry, strike, option_type, total_quantity, average_cost, status)"
        " VALUES (1,'SPY','2030-01-01',750,'C',1,1.0,'open')"
    )
    old.commit()
    old.close()

    monkeypatch.setattr(core_db, "DB_PATH", str(path))
    core_db.init_db()

    conn = core_db.get_connection()
    try:
        cols = lambda t: {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}
        assert "add_without_parent" in cols("positions")
        assert "position_id" in cols("trades")
        assert "tweet_created_at" in cols("alerts")
        assert "latency_sec" in cols("decisions")
        # Pre-existing rows survive the upgrade.
        assert conn.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"] == 1
    finally:
        conn.close()

    core_db.init_db()  # idempotent on an already-migrated database


def test_positions_table_has_add_without_parent(db):
    columns = {r["name"] for r in db.execute("PRAGMA table_info(positions)").fetchall()}
    assert "add_without_parent" in columns


def test_buy_fill_joins_the_callers_transaction(db, monkeypatch, tmp_path):
    """process_buy_fill must reuse the caller's connection. Opening its own while
    the caller holds a write lock blocks under WAL and then throws."""
    import src.core.db as core_db
    import src.services.executor as executor

    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "test.db"))
    db.execute("INSERT INTO users (username, password_hash) VALUES ('alice', 'h')")
    db.execute(
        "INSERT INTO user_configs (user_id, config_json) VALUES (1, '{\"execution\": {\"paper_mode\": true}}')"
    )
    db.commit()

    # Hold an open write transaction on the caller's connection, exactly as
    # process_open_orders does after reconciling an expired position.
    db.execute("UPDATE users SET is_active = 1 WHERE id = 1")

    notifications = executor.process_buy_fill(
        user_id=1, decision_id=1, ticker="SPY", expiry="2030-12-20", strike=750,
        option_type="C", signal_action="BTO", fill_price=1.00, contracts=1,
        paper_mode=True, order_id="b1", conn=db,
    )
    db.commit()

    assert db.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"] == 1
    # Notifications are deferred, not fired inside the transaction.
    assert len(notifications) == 2


def test_trade_is_linked_to_its_position(db):
    columns = {r["name"] for r in db.execute("PRAGMA table_info(trades)").fetchall()}
    assert "position_id" in columns


def test_expired_positions_are_selected_for_reconciliation(db):
    db.execute("INSERT INTO users (username, password_hash) VALUES ('alice', 'h')")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for expiry in (yesterday, tomorrow):
        db.execute(
            "INSERT INTO positions (user_id, ticker, expiry, strike, option_type, total_quantity, average_cost, status)"
            " VALUES (1, 'SPY', ?, 750, 'C', 1, 1.00, 'open')",
            (expiry,),
        )
    db.commit()

    rows = db.execute(
        "SELECT expiry FROM positions WHERE status = 'open' AND expiry < ?",
        (date.today().isoformat(),),
    ).fetchall()
    assert [r["expiry"] for r in rows] == [yesterday]
