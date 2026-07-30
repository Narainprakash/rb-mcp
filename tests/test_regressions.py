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


def test_robinhood_provider_without_credentials_fails_closed(rh_env, monkeypatch):
    """Superseded the old 'not implemented' assertion: the provider now works,
    but must still refuse rather than fall back when it cannot authenticate."""
    import src.services.robinhood_instruments as instruments
    from src.services.quotes import QuoteUnavailable, get_quote
    from src.services.robinhood_mcp import MCPCallFailed

    def unauthenticated(tool, arguments, user_id=None, **kwargs):
        raise MCPCallFailed("not authenticated to Robinhood; run scripts/robinhood_login.py")

    monkeypatch.setattr(instruments, "call_tool", unauthenticated)
    with pytest.raises(QuoteUnavailable):
        get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)


# --- Robinhood MCP quote provider ------------------------------------------

@pytest.fixture
def rh_env(tmp_path, monkeypatch):
    """Robinhood provider with a mockable MCP transport and a real DB."""
    import src.core.db as core_db
    import src.services.broker_limits as limits
    import src.services.quotes as quotes
    import src.services.robinhood_mcp as rh

    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "rh.db"))
    core_db.init_db()
    monkeypatch.setattr(rh, "have_credentials", lambda user_id: True)
    limits.reset()
    quotes.clear_cache()

    def install(handler):
        # robinhood_instruments binds call_tool at import time, so patching only
        # robinhood_mcp would leave the instrument lookup hitting the network.
        import src.services.robinhood_instruments as instruments
        monkeypatch.setattr(rh, "call_tool", handler)
        monkeypatch.setattr(instruments, "call_tool", handler)

    return install


def _responder(instrument_result, quote_result=None):
    """Builds an MCP handler; an Exception value is raised instead of returned."""
    seen = []

    def handler(tool, arguments, user_id=None, **kwargs):
        seen.append(tool)
        source = instrument_result if tool == "get_option_instruments" else quote_result
        if isinstance(source, Exception):
            raise source
        return source

    handler.seen = seen
    return handler


def test_mcp_envelope_is_unwrapped():
    """Every Robinhood tool wraps its payload as {"data": ..., "guide": "..."}.

    Reading payload keys at the top level - what the schema descriptions implied
    - silently finds nothing, which is why every alert on 2026-07-29 skipped
    with "no tradable contract found" despite a correct query.
    """
    from src.services.robinhood_mcp import unwrap_envelope

    enveloped = {
        "data": {"instruments": [{"id": "uuid-1", "strike_price": "736.0000"}]},
        "guide": "LLM-facing prose that must not be mistaken for payload",
    }
    assert unwrap_envelope(enveloped) == {"instruments": [{"id": "uuid-1", "strike_price": "736.0000"}]}

    # Unenveloped payloads and lists must pass through untouched.
    assert unwrap_envelope({"instruments": []}) == {"instruments": []}
    assert unwrap_envelope([1, 2]) == [1, 2]


def test_robinhood_quote_returns_real_bid_ask(rh_env):
    from src.services.quotes import get_quote

    handler = _responder({"instruments": [{"id": "uuid-1"}]},
                         {"quotes": [{"bid_price": "1.80", "ask_price": "1.86"}]})
    rh_env(handler)

    quote = get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)
    assert quote == {"bid": 1.80, "ask": 1.86}


def test_instrument_uuid_is_cached_across_quotes(rh_env):
    """A contract's UUID never changes, so only the first quote should pay for
    the lookup. Without this the monitor triples its broker usage."""
    from src.services.quotes import get_quote

    handler = _responder({"instruments": [{"id": "uuid-1"}]},
                         {"quotes": [{"bid_price": "1.80", "ask_price": "1.86"}]})
    rh_env(handler)

    get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)
    assert handler.seen == ["get_option_instruments", "get_option_quotes"]

    handler.seen.clear()
    get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)
    assert handler.seen == ["get_option_quotes"]


@pytest.mark.parametrize("label,instruments,quote", [
    ("ambiguous match", {"instruments": [{"id": "a"}, {"id": "b"}]}, None),
    ("no contract", {"instruments": []}, None),
    ("crossed market", {"instruments": [{"id": "c"}]},
     {"quotes": [{"bid_price": "2.00", "ask_price": "1.00"}]}),
    ("zero ask", {"instruments": [{"id": "d"}]},
     {"quotes": [{"bid_price": "0", "ask_price": "0"}]}),
    ("malformed payload", {"instruments": [{"id": "e"}]}, {"quotes": [{"foo": "bar"}]}),
    ("empty quotes", {"instruments": [{"id": "f"}]}, {"quotes": []}),
])
def test_bad_market_data_fails_closed(rh_env, label, instruments, quote):
    """Every bad-data path must skip the signal, never invent or guess a price."""
    from src.services.quotes import QuoteUnavailable, get_quote

    rh_env(_responder(instruments, quote))
    with pytest.raises(QuoteUnavailable):
        get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)


def test_mcp_failure_fails_closed(rh_env):
    from src.services.quotes import QuoteUnavailable, get_quote
    from src.services.robinhood_mcp import MCPCallFailed

    rh_env(_responder(MCPCallFailed("token expired: 401")))
    with pytest.raises(QuoteUnavailable) as excinfo:
        get_quote("SPY", "2026-08-21", 750, "C", 1.81, source="robinhood", cache_ttl_sec=0, user_id=1)
    assert "401" in str(excinfo.value)


def test_account_must_be_agentic_and_options_enabled(rh_env):
    """The tool docs require agentic_allowed plus option level 2/3 before any
    order. Enforcing it here turns a misconfigured account into a clean skip."""
    import src.services.robinhood_mcp as rh

    cases = [
        ({"accounts": [{"account_number": "X1", "agentic_allowed": True,
                        "option_level": "option_level_2"}]}, True),
        ({"accounts": [{"account_number": "X1", "agentic_allowed": False,
                        "option_level": "option_level_3"}]}, False),
        ({"accounts": [{"account_number": "X1", "agentic_allowed": True,
                        "option_level": "option_level_0"}]}, False),
        ({"accounts": [{"account_number": "X1", "agentic_allowed": True,
                        "option_level": ""}]}, False),
        ({"accounts": [{"account_number": "OTHER", "agentic_allowed": True,
                        "option_level": "option_level_2"}]}, False),
    ]
    for payload, expected in cases:
        rh_env(lambda tool, args, user_id=None, **kw: payload)
        ok, reason = rh.account_is_tradeable("X1", user_id=1)
        assert ok is expected, f"{payload} -> {reason}"


# --- Per-user Robinhood credentials ----------------------------------------

def test_credentials_are_isolated_per_user(tmp_path, monkeypatch):
    """Tenants are different people with their own Robinhood logins, so one
    user's token must never satisfy another's authentication check."""
    import asyncio

    import src.services.robinhood_auth as auth
    from mcp.shared.auth import OAuthToken

    monkeypatch.setattr(auth, "PROJECT_ROOT", str(tmp_path))

    assert auth.token_path(1) != auth.token_path(2)
    assert not auth.have_credentials(1)
    assert not auth.have_credentials(2)

    asyncio.run(auth.FileTokenStorage(1).set_tokens(
        OAuthToken(access_token="user-one-token", token_type="Bearer")))

    assert auth.have_credentials(1)
    assert not auth.have_credentials(2), "user 2 must not inherit user 1's login"


def test_legacy_token_migrates_only_when_owner_is_unambiguous(tmp_path, monkeypatch):
    """The original design wrote one process-wide token. Adopting it is safe
    only when a single user exists; otherwise its owner is unknowable."""
    import src.core.db as core_db
    import src.services.robinhood_auth as auth

    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(auth, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(auth, "LEGACY_TOKEN_PATH", str(tmp_path / ".robinhood_token.json"))
    core_db.init_db()

    conn = core_db.get_connection()
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('solo','h')")
    conn.commit()
    conn.close()

    with open(auth.LEGACY_TOKEN_PATH, "w") as handle:
        handle.write('{"tokens": {"access_token": "legacy"}}')

    assert auth.migrate_legacy_token() == 1
    assert auth.have_credentials(1)
    assert not os.path.exists(auth.LEGACY_TOKEN_PATH)

    # With a second user present, an unclaimed legacy file must be left alone.
    conn = core_db.get_connection()
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('second','h')")
    conn.commit()
    conn.close()
    with open(auth.LEGACY_TOKEN_PATH, "w") as handle:
        handle.write('{"tokens": {"access_token": "legacy"}}')

    assert auth.migrate_legacy_token() is None
    assert os.path.exists(auth.LEGACY_TOKEN_PATH)


# --- Broker API budget -----------------------------------------------------

@pytest.fixture
def fake_provider(monkeypatch):
    """Registers a counting network-backed provider."""
    import src.services.quotes as quotes
    import src.services.broker_limits as limits

    calls = {"n": 0}

    def provider(ticker, expiry, strike, option_type, reference_price, user_id=None):
        calls["n"] += 1
        return {"bid": 1.00, "ask": 1.05}

    monkeypatch.setitem(quotes.PROVIDERS, "fake", provider)
    monkeypatch.setitem(quotes.PROVIDER_META, "fake",
                        {"implemented": True, "live_data": True, "label": "fake"})
    monkeypatch.setattr(quotes, "log_broker_call", lambda endpoint: None)
    limits.reset()
    quotes.clear_cache()
    yield calls
    limits.reset()
    quotes.clear_cache()


def test_rate_limiter_caps_broker_calls(fake_provider):
    """The cap must hold regardless of how often the loops ask."""
    from src.services.quotes import QuoteUnavailable, get_quote

    succeeded = refused = 0
    for strike in range(200):
        try:
            get_quote("SPY", "2030-01-01", strike, "C", 1.0,
                      source="fake", cache_ttl_sec=0, max_calls_per_min=60, user_id=1)
            succeeded += 1
        except QuoteUnavailable:
            refused += 1

    assert succeeded == 60
    assert refused == 140
    assert fake_provider["n"] == 60, "provider was called past the cap"


def test_quote_cache_dedups_identical_contracts(fake_provider):
    """Ten users holding one contract must cost one call, not ten."""
    from src.services.quotes import get_quote

    for _ in range(10):
        get_quote("SPY", "2030-01-01", 750, "C", 1.0,
                  source="fake", cache_ttl_sec=30, max_calls_per_min=60, user_id=1)

    assert fake_provider["n"] == 1


def test_simulated_provider_does_not_spend_broker_budget(fake_provider):
    """Paper mode is local; it must never consume the shared broker budget."""
    import src.services.broker_limits as limits
    from src.services.quotes import get_quote

    for _ in range(50):
        get_quote("SPY", "2030-01-01", 750, "C", 1.81,
                  source="simulated", cache_ttl_sec=10, max_calls_per_min=60)

    assert limits.current_usage()[0] == 0


def test_rate_limited_quote_fails_closed(fake_provider):
    """Exhausting the budget must surface as QuoteUnavailable so the caller
    skips the signal, rather than any partial or invented price."""
    from src.services.quotes import QuoteUnavailable, get_quote

    for strike in range(60):
        get_quote("SPY", "2030-01-01", strike, "C", 1.0,
                  source="fake", cache_ttl_sec=0, max_calls_per_min=60, user_id=1)

    with pytest.raises(QuoteUnavailable) as excinfo:
        get_quote("SPY", "2030-01-01", 999, "C", 1.0,
                  source="fake", cache_ttl_sec=0, max_calls_per_min=60, user_id=1)
    assert "rate limit" in str(excinfo.value).lower()


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


# --- Dashboard: live-mode confirmation gate --------------------------------

@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    import src.core.db as core_db

    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "d.db"))
    core_db.init_db()
    conn = core_db.get_connection()
    conn.execute("INSERT INTO users (username, password_hash, is_active) VALUES ('admin','h',1)")
    conn.commit()
    conn.close()

    import src.dashboard.app as dash
    dash.app.config["TESTING"] = True
    client = dash.app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True
    return client, dash


def test_going_live_requires_typed_confirmation(dash_client, monkeypatch):
    client, dash = dash_client
    monkeypatch.setattr(dash, "notify_mode_change", lambda *a: None)

    res = client.post("/api/settings", json={"execution": {"paper_mode": False}})
    assert res.status_code == 400
    assert "LIVE" in res.get_json()["errors"][0]

    # And nothing was persisted.
    res = client.get("/api/settings")
    assert res.get_json()["execution"]["paper_mode"] is True


def test_going_live_with_confirmation_audits_and_notifies(dash_client, monkeypatch):
    import src.core.db as core_db

    client, dash = dash_client
    sent = []
    monkeypatch.setattr(dash, "notify_mode_change", lambda *a: sent.append(a))

    res = client.post("/api/settings", json={"execution": {"paper_mode": False}, "confirm": "LIVE"})
    assert res.status_code == 200
    assert res.get_json()["paper_mode"] is False
    assert sent and sent[0][1] is True  # going_live=True

    conn = core_db.get_connection()
    try:
        row = conn.execute(
            "SELECT message FROM system_events WHERE event_type = 'mode_change'"
        ).fetchone()
        assert row is not None and "LIVE" in row["message"]
    finally:
        conn.close()


def test_returning_to_paper_needs_no_confirmation(dash_client, monkeypatch):
    client, dash = dash_client
    monkeypatch.setattr(dash, "notify_mode_change", lambda *a: None)

    client.post("/api/settings", json={"execution": {"paper_mode": False}, "confirm": "LIVE"})
    res = client.post("/api/settings", json={"execution": {"paper_mode": True}})
    assert res.status_code == 200
    assert res.get_json()["paper_mode"] is True


def test_unchanged_mode_is_not_reported_as_a_change(dash_client, monkeypatch):
    client, dash = dash_client
    sent = []
    monkeypatch.setattr(dash, "notify_mode_change", lambda *a: sent.append(a))

    res = client.post("/api/settings", json={"execution": {"paper_mode": True},
                                             "decision": {"take_profit_pct": 25}})
    assert res.status_code == 200
    assert res.get_json()["paper_mode"] is None
    assert sent == []


# --- Ops: backups ----------------------------------------------------------

def test_backup_is_consistent_under_an_open_write(tmp_path, monkeypatch):
    """The online backup API must exclude uncommitted work. A file copy under
    WAL can capture a torn state; this is why we don't use one."""
    import src.core.db as core_db
    from src.core.backup import backup_database

    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "live.db"))
    core_db.init_db()
    conn = core_db.get_connection()
    conn.execute("INSERT INTO alerts (tweet_id, raw_text, parse_status) VALUES ('1','committed','success')")
    conn.commit()

    writer = core_db.get_connection()
    writer.execute("INSERT INTO alerts (tweet_id, raw_text, parse_status) VALUES ('2','uncommitted','success')")

    path = backup_database(core_db.DB_PATH, str(tmp_path / "backups"), retention=5)

    writer.rollback()
    writer.close()
    conn.close()

    snap = sqlite3.connect(path)
    try:
        assert snap.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    finally:
        snap.close()


def test_backup_rotation_keeps_only_retention(tmp_path):
    from src.core.backup import prune_backups

    dest = tmp_path / "backups"
    dest.mkdir()
    for stamp in ["20260101-000001", "20260102-000001", "20260103-000001", "20260104-000001"]:
        (dest / f"hermes_mt-{stamp}.db").write_text("x")
    (dest / "unrelated.txt").write_text("keep me")

    prune_backups(str(dest), retention=2)

    remaining = sorted(p.name for p in dest.iterdir())
    assert remaining == ["hermes_mt-20260103-000001.db", "hermes_mt-20260104-000001.db", "unrelated.txt"]


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
