"""Regression tests for the defects found in the July 2026 code review."""
import os
import sqlite3
from datetime import date, timedelta

import pytest

from src.core.db import SCHEMA
from src.core.time_utils import get_ny_time
from src.services.executor import get_live_quote
from src.services.parser import parse_alert


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
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
    with pytest.raises(ValueError):
        get_live_quote("SPY", "2026-08-21", 750, "C", None)


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

def test_positions_table_has_add_without_parent(db):
    columns = {r["name"] for r in db.execute("PRAGMA table_info(positions)").fetchall()}
    assert "add_without_parent" in columns


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
