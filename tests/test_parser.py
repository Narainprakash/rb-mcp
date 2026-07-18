import pytest
from datetime import date
from src.services.parser import parse_alert

def test_parse_bto_swing():
    tweet = """#ALERT
BTO $SPY 7/15 752C
1.81
SWING"""
    signal = parse_alert(tweet)
    assert signal.parse_status == "success"
    assert signal.action == "BTO"
    assert signal.ticker == "SPY"
    assert signal.strike == 752.0
    assert signal.option_type == "C"
    assert signal.price == 1.81
    assert "SWING" in signal.trade_style
    # Expiry validation depends on today's date, just ensure it parsed
    assert "07-15" in signal.expiry

def test_parse_add_average():
    tweet = """#ALERT (ADD)
$SPY 7/15 754C
AVG: .98"""
    signal = parse_alert(tweet)
    assert signal.parse_status == "success"
    assert signal.action == "ADD"
    assert signal.ticker == "SPY"
    assert signal.strike == 754.0
    assert signal.option_type == "C"
    assert signal.price == 0.98

def test_parse_0dte():
    # For 0DTE, it should set expiry to today
    tweet = """#ALERT
BTO $QQQ 1/1 400P
2.50
0DTE"""
    signal = parse_alert(tweet)
    assert signal.parse_status == "success"
    assert signal.action == "BTO"
    assert signal.ticker == "QQQ"
    assert signal.strike == 400.0
    assert signal.option_type == "P"
    assert signal.price == 2.5
    assert "0DTE" in signal.trade_style
    today = date.today()
    assert signal.expiry == f"{today.year}-{today.month:02d}-{today.day:02d}"

def test_invalid_missing_price():
    tweet = """#ALERT
BTO $SPY 7/15 752C
SWING"""
    signal = parse_alert(tweet)
    assert signal.parse_status == "needs_review"
    assert signal.price is None
