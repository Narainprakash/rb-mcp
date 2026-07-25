"""
Hard ceiling on outbound broker (Robinhood) API calls.

The trading loops are O(users x open orders) per pass. At the default 15s
monitor interval that is ~1,740 passes across a session, so a handful of open
positions becomes thousands of calls a day and ten users becomes six figures.
Rate limits and account flagging are a real risk at that volume.

This module makes the ceiling structural rather than a matter of the loops
being careful: every broker call goes through `consume()`, and when the budget
is gone the call is refused. A future change to the monitor cannot reintroduce
the problem without deliberately bypassing this.

Refusing fails *closed* - callers raise QuoteUnavailable and the signal is
skipped. Skipping a trade is recoverable; getting the account restricted is not.
"""
import threading
import time
from collections import deque

# Robinhood does not publish limits for the Agentic MCP, so this default is a
# deliberately conservative guess, not a documented figure. Tune it against
# their actual limits once the integration is live.
DEFAULT_CALLS_PER_MIN = 60

_lock = threading.Lock()
_calls = deque()          # timestamps of recent calls
_refused = 0              # refusals since process start, for observability


class RateLimitExceeded(Exception):
    """Budget exhausted. Callers must skip, never retry in a tight loop."""


def consume(max_per_min=DEFAULT_CALLS_PER_MIN, endpoint="unknown"):
    """Claims one call from the budget, or raises RateLimitExceeded.

    Sliding window rather than a fixed bucket, so a burst at a minute boundary
    cannot spend two windows' worth of budget at once.
    """
    global _refused
    now = time.monotonic()
    with _lock:
        cutoff = now - 60
        while _calls and _calls[0] < cutoff:
            _calls.popleft()

        if len(_calls) >= max_per_min:
            _refused += 1
            raise RateLimitExceeded(
                f"broker rate limit reached ({len(_calls)}/{max_per_min} calls in the last 60s); "
                f"refused call to {endpoint}"
            )

        _calls.append(now)
        return len(_calls)


def current_usage():
    """(calls in the last 60s, refusals since start) - for the dashboard."""
    now = time.monotonic()
    with _lock:
        cutoff = now - 60
        while _calls and _calls[0] < cutoff:
            _calls.popleft()
        return len(_calls), _refused


def reset():
    """Test helper."""
    global _refused
    with _lock:
        _calls.clear()
        _refused = 0
