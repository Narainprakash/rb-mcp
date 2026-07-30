#!/usr/bin/env python
"""
Read-only diagnostic for the option contract lookup.

Every alert on 2026-07-29 was skipped with "no tradable contract found", which
means resolve_instrument_id() could not find the contract. The response shape
was inferred from the tool's schema description rather than a live response, so
this dumps what the server actually returns.

It runs a ladder of progressively looser queries, so the failing filter is
obvious: if the exact query returns nothing but a looser one succeeds, the
offending parameter is whichever was dropped.

Read-only - only calls get_option_chains / get_option_instruments /
get_option_quotes. It cannot place an order.

    venv/bin/python scripts/debug_option_lookup.py SPY 2026-07-31 736 C
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.db import get_connection, init_db  # noqa: E402
from src.services.robinhood_mcp import MCPCallFailed, call_tool  # noqa: E402


def only_user_id():
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, username FROM users WHERE is_active = 1 ORDER BY id").fetchall()
    finally:
        conn.close()
    if not rows:
        raise SystemExit("No active users.")
    print(f"Using user {rows[0]['id']} ({rows[0]['username']})")
    return rows[0]["id"]


def attempt(label, tool, arguments, user_id):
    print(f"\n{'-' * 70}\n{label}\n  tool: {tool}\n  args: {json.dumps(arguments)}")
    try:
        data = call_tool(tool, arguments, user_id=user_id)
    except MCPCallFailed as e:
        print(f"  FAILED: {e}")
        return None

    if isinstance(data, dict):
        print(f"  top-level keys: {list(data.keys())}")
        for key, value in data.items():
            if isinstance(value, list):
                print(f"    '{key}': list of {len(value)}")
            else:
                print(f"    '{key}': {type(value).__name__}")
    else:
        print(f"  top-level type: {type(data).__name__}")

    # The whole point: show the real structure rather than trusting a guess.
    text = json.dumps(data, indent=2)
    print("  raw response (first 1600 chars):")
    for line in text[:1600].splitlines():
        print(f"    {line}")
    if len(text) > 1600:
        print(f"    ... [{len(text) - 1600} more chars]")
    return data


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 1
    ticker, expiry, strike, option_type = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    api_type = {"C": "call", "P": "put"}[option_type.upper()]
    user_id = only_user_id()

    print(f"\nLooking up {ticker} {expiry} {strike}{option_type.upper()} ({api_type})")

    # What the chain looks like - also shows whether several chains exist for
    # this underlying, which would change how instruments must be queried.
    attempt("1. Chains for the underlying", "get_option_chains",
            {"underlying_symbol": ticker.upper()}, user_id)

    # Exactly what resolve_instrument_id() sends today.
    attempt("2. EXACT query (what the bot currently sends)", "get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        "strike_price": f"{float(strike):.4f}",
        "type": api_type,
        "state": "active",
        "tradability": "tradable",
    }, user_id)

    # Drop one filter at a time to find the culprit.
    attempt("3. Without tradability", "get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        "strike_price": f"{float(strike):.4f}",
        "type": api_type,
        "state": "active",
    }, user_id)

    attempt("4. Without state or tradability", "get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        "strike_price": f"{float(strike):.4f}",
        "type": api_type,
    }, user_id)

    # Is the strike *format* the problem? '736' vs '736.0000'.
    attempt("5. Unpadded strike", "get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        "strike_price": str(int(float(strike))) if float(strike).is_integer() else str(strike),
        "type": api_type,
    }, user_id)

    # The quote response shape matters just as much: it is parsed by the same
    # inference that got the instruments payload wrong.
    exact = attempt("6. EXACT query again, to capture the instrument id", "get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        "strike_price": f"{float(strike):.4f}",
        "type": api_type,
        "state": "active",
        "tradability": "tradable",
    }, user_id)

    instrument_id = None
    if isinstance(exact, dict):
        candidates = exact.get("instruments") or (exact.get("data") or {}).get("instruments") or []
        if candidates:
            instrument_id = candidates[0].get("id")

    if instrument_id:
        print(f"\n  -> resolved instrument id: {instrument_id}")
        attempt("7. QUOTE for that instrument (verifies bid/ask field names)",
                "get_option_quotes", {"instrument_ids": [instrument_id]}, user_id)
    else:
        print("\n  Could not extract an instrument id, so the quote step is skipped.")

    print(f"\n{'=' * 70}\nWhat matters here: the exact key path to the payload, and the")
    print("field names carrying bid/ask in step 7.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
