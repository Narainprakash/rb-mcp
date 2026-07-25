"""
Option quote sources.

Quoting and order execution are separable concerns: a read-only quote feed can
be wired up long before order placement is, and doing so is what makes a paper
run mean anything. This module keeps that seam explicit.

Select the active provider with `execution.quote_source` in config.yaml.
"""
import hashlib
import threading
import time
from datetime import datetime

from src.core.time_utils import NY_TZ
from src.services.broker_limits import (
    DEFAULT_CALLS_PER_MIN,
    RateLimitExceeded,
    consume,
)


class QuoteUnavailable(Exception):
    """Raised when the configured provider cannot supply a quote. Callers must
    treat this as 'do not trade', never as a reason to guess a price."""


def _simulated_quote(ticker, expiry, strike, option_type, reference_price):
    """
    SIMULATED quote source. This is NOT market data.

    Derives a bid/ask deterministically from `reference_price` (the alert price
    for entries, the position's average cost for exits) plus a drift that is a
    pure function of the contract and the current minute. Reproducible rather
    than random, and anchored to the contract actually being traded - but
    simulated P/L says nothing about how the strategy would perform on real
    prices. Use it to exercise the pipeline, not to evaluate the strategy.
    """
    if reference_price is None or reference_price <= 0:
        raise QuoteUnavailable(
            f"reference_price required for simulated quote of {ticker} {strike}{option_type}"
        )

    bucket = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M")
    seed = f"{ticker}|{expiry}|{strike}|{option_type}|{bucket}"
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    drift_pct = ((digest % 3001) / 10000.0) - 0.15

    mid = max(0.01, reference_price * (1 + drift_pct))
    spread = max(0.01, round(mid * 0.02, 2))
    return {"bid": round(mid - spread / 2, 2), "ask": round(mid + spread / 2, 2)}


def _robinhood_quote(ticker, expiry, strike, option_type, reference_price):
    """Real quotes from the Robinhood Agentic Trading MCP.

    `reference_price` is unused here - it exists for the simulated provider.
    Real prices come from the broker, which is the entire point.
    """
    from src.services.robinhood_instruments import resolve_instrument_id
    from src.services.robinhood_mcp import MCPCallFailed, call_tool

    try:
        instrument_id = resolve_instrument_id(ticker, expiry, strike, option_type)
        data = call_tool("get_option_quotes", {"instrument_ids": [instrument_id]})
    except MCPCallFailed as e:
        # Surfaced as QuoteUnavailable so the caller skips the signal. Never
        # fall back to a simulated price: silently trading on invented data is
        # worse than not trading.
        raise QuoteUnavailable(str(e))

    quotes = data.get("quotes") if isinstance(data, dict) else data
    if not isinstance(quotes, list) or not quotes:
        raise QuoteUnavailable(f"no quote returned for {ticker} {strike}{option_type}")

    quote = quotes[0]
    try:
        bid = float(quote["bid_price"])
        ask = float(quote["ask_price"])
    except (KeyError, TypeError, ValueError) as e:
        raise QuoteUnavailable(
            f"unexpected quote shape for {ticker} {strike}{option_type}: {e}"
        )

    if ask <= 0 or bid < 0 or bid > ask:
        # A crossed or zero market means no tradeable price - usually a halt or
        # an illiquid contract. Decisioning on it would be meaningless.
        raise QuoteUnavailable(
            f"unusable market for {ticker} {strike}{option_type}: bid {bid}, ask {ask}"
        )

    return {"bid": bid, "ask": ask}


PROVIDERS = {
    "simulated": _simulated_quote,
    "robinhood": _robinhood_quote,
}

# Declared capability per provider, so status displays report what is actually
# true rather than inferring it from config intent. `implemented` means this
# process can genuinely obtain a quote; `live_data` means those quotes are real
# market prices. A provider must never be shown as working because someone
# selected it in config.
PROVIDER_META = {
    "simulated": {"implemented": True, "live_data": False,
                  "label": "Simulated (not market data)"},
    "robinhood": {"implemented": True, "live_data": True,
                  "label": "Robinhood Agentic MCP"},
}


def provider_meta(source):
    return PROVIDER_META.get(
        source, {"implemented": False, "live_data": False, "label": str(source)}
    )


_cache_lock = threading.Lock()
_cache = {}  # contract key -> (expires_at_monotonic, quote)


def _cache_key(ticker, expiry, strike, option_type):
    return f"{ticker}|{expiry}|{strike}|{option_type}"


def clear_cache():
    """Test helper."""
    with _cache_lock:
        _cache.clear()


def get_quote(ticker, expiry, strike, option_type, reference_price,
              source="simulated", cache_ttl_sec=0, max_calls_per_min=None):
    """Returns {'bid': float, 'ask': float} from the configured provider.

    For network-backed providers the call is deduplicated through a short-TTL
    cache and then charged against the broker rate limit. Both matter because
    the monitor quotes every open order of every user on each pass: without the
    cache, ten users holding the same contract cost ten identical calls; without
    the limiter, nothing bounds the total.

    The simulated provider is local, so it neither caches nor spends budget.
    """
    provider = PROVIDERS.get(source)
    if provider is None:
        raise QuoteUnavailable(
            f"unknown quote_source '{source}' (known: {', '.join(sorted(PROVIDERS))})"
        )

    meta = provider_meta(source)
    if not meta.get("live_data"):
        return provider(ticker, expiry, strike, option_type, reference_price)

    key = _cache_key(ticker, expiry, strike, option_type)
    now = time.monotonic()

    if cache_ttl_sec > 0:
        with _cache_lock:
            entry = _cache.get(key)
            if entry and entry[0] > now:
                return entry[1]

    limit = max_calls_per_min if max_calls_per_min is not None else DEFAULT_CALLS_PER_MIN
    try:
        consume(max_per_min=limit, endpoint=f"quote:{source}")
    except RateLimitExceeded as e:
        # Fail closed. Skipping a signal is recoverable; a restricted brokerage
        # account is not.
        raise QuoteUnavailable(str(e))

    log_broker_call(f"quote:{ticker}{strike}{option_type}")
    quote = provider(ticker, expiry, strike, option_type, reference_price)

    if cache_ttl_sec > 0:
        with _cache_lock:
            _cache[key] = (now + cache_ttl_sec, quote)

    return quote


def log_broker_call(endpoint):
    """Records a Robinhood call in api_calls so the dashboard reflects real
    broker usage, not just X API usage."""
    try:
        from src.core.db import get_connection
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO api_calls (service, endpoint) VALUES ('robinhood', ?)",
                (endpoint,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"WARNING: could not log broker API call: {e}")
