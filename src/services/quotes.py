"""
Option quote sources.

Quoting and order execution are separable concerns: a read-only quote feed can
be wired up long before order placement is, and doing so is what makes a paper
run mean anything. This module keeps that seam explicit.

Select the active provider with `execution.quote_source` in config.yaml.
"""
import hashlib
from datetime import datetime

from src.core.time_utils import NY_TZ


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

    Not implemented yet, and blocked on two things (specs.md section 0.2):
    options support on the MCP is unconfirmed, and the MCP is an agent-tool
    interface bound to the Hermes Agent's OAuth session rather than a REST API
    this process can call. Fails closed rather than falling back to simulated
    prices, so a misconfiguration cannot quietly trade on invented data.
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


def get_quote(ticker, expiry, strike, option_type, reference_price, source="simulated"):
    """Returns {'bid': float, 'ask': float} from the configured provider."""
    provider = PROVIDERS.get(source)
    if provider is None:
        raise QuoteUnavailable(
            f"unknown quote_source '{source}' (known: {', '.join(sorted(PROVIDERS))})"
        )
    return provider(ticker, expiry, strike, option_type, reference_price)
