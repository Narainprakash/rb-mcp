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
    """
    Placeholder for quotes from the Robinhood Agentic Trading MCP - the right
    long-term source, since quoting the same venue we execute on removes any
    basis risk between the decision price and the fill price.

    Not implemented yet. Two blockers, one of them now confirmed:

    1. Options support on the MCP is unconfirmed (specs.md section 0.2).
    2. There is no way for this process to invoke an MCP tool. Checked against
       the CLI on 2026-07-25: `hermes mcp` offers only connection management
       (add / remove / list / test / configure / login / reauth / catalog /
       install / serve) with no call or invoke subcommand, so the shell-out
       route used for `hermes send` does not exist for MCP tools. `serve` is
       the reverse direction - it exposes Hermes to other agents.

    The remaining viable path is a real MCP client in this process, reusing the
    OAuth token the agent has already cached. Do NOT route quotes or orders
    through the agent conversationally (`hermes send` and parse the reply):
    that puts an LLM in the deterministic trading path, which section 1 of the
    spec exists to prevent, and would make a hallucinated price into a trade.

    Fails closed rather than falling back to simulated prices, so a
    misconfiguration cannot quietly trade on invented data.
    """
    raise QuoteUnavailable(
        "quote_source 'robinhood' is not implemented yet - no MCP client in this process. "
        "Set execution.quote_source to 'simulated' or wire a provider."
    )


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
    "robinhood": {"implemented": False, "live_data": True,
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
