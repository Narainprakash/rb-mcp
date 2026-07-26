"""
Resolving a contract to its Robinhood option instrument UUID.

`get_option_quotes` takes instrument UUIDs, not ticker/strike/expiry, so every
quote would otherwise cost a three-call chain:

    get_option_chains -> get_option_instruments -> get_option_quotes

At the monitor's cadence that triples broker usage for no benefit, because a
contract's UUID never changes. Resolutions are therefore cached permanently in
SQLite: the first quote for a contract costs two calls, every later one costs
one.
"""
from src.core.db import get_connection
from src.services.robinhood_mcp import MCPCallFailed, call_tool


def _cached(ticker, expiry, strike, option_type):
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT instrument_id FROM option_instruments
            WHERE ticker = ? AND expiry = ? AND strike = ? AND option_type = ?
        """, (ticker.upper(), expiry, float(strike), option_type.upper())).fetchone()
        return row["instrument_id"] if row else None
    finally:
        conn.close()


def _remember(ticker, expiry, strike, option_type, instrument_id):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO option_instruments (ticker, expiry, strike, option_type, instrument_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker, expiry, strike, option_type)
            DO UPDATE SET instrument_id = excluded.instrument_id
        """, (ticker.upper(), expiry, float(strike), option_type.upper(), instrument_id))
        conn.commit()
    finally:
        conn.close()


def resolve_instrument_id(ticker, expiry, strike, option_type, user_id):
    """Returns the option instrument UUID, or raises MCPCallFailed.

    `expiry` is ISO (YYYY-MM-DD) as stored on alerts; `option_type` is the
    parser's 'C'/'P', mapped here to the API's 'call'/'put'.

    The lookup is billed to `user_id`'s Robinhood login, but the *result* is
    cached globally: an option's instrument UUID is a property of the contract,
    identical for every account, so re-resolving it per user would waste calls
    against a shared budget.
    """
    cached = _cached(ticker, expiry, strike, option_type)
    if cached:
        return cached

    api_type = {"C": "call", "P": "put"}.get(option_type.upper())
    if not api_type:
        raise MCPCallFailed(f"unknown option type {option_type!r}")

    data = call_tool("get_option_instruments", {
        "chain_symbol": ticker.upper(),
        "expiration_dates": expiry,
        # The API wants a fixed-precision string, e.g. '750.0000'.
        "strike_price": f"{float(strike):.4f}",
        "type": api_type,
        "state": "active",
        "tradability": "tradable",
    }, user_id=user_id)

    instruments = data.get("instruments") if isinstance(data, dict) else data
    if not isinstance(instruments, list) or not instruments:
        raise MCPCallFailed(
            f"no tradable contract found for {ticker} {expiry} {strike}{option_type}"
        )

    # Filters were exact, so a single match is expected. More than one means the
    # assumption is wrong somewhere - refuse rather than pick arbitrarily, since
    # the wrong UUID is the wrong contract.
    if len(instruments) > 1:
        raise MCPCallFailed(
            f"{len(instruments)} contracts matched {ticker} {expiry} {strike}{option_type}; "
            "refusing to guess which one"
        )

    instrument_id = instruments[0].get("id")
    if not instrument_id:
        raise MCPCallFailed("contract lookup returned no instrument id")

    _remember(ticker, expiry, strike, option_type, instrument_id)
    return instrument_id
