"""
Synchronous access to the Robinhood Agentic Trading MCP.

The trading pipeline runs in plain threads while the MCP SDK is asyncio-only,
so this module owns a background event loop and exposes a blocking `call_tool`.

Every call is charged against the shared broker budget (broker_limits) and
recorded in api_calls. Failures raise MCPCallFailed; callers translate that into
"skip this signal" rather than guessing a price or retrying in a tight loop.

Connection strategy: one connection per call. A persistent session would avoid
the handshake, but it also has to survive token refresh, idle timeouts and
reconnects across threads - complexity that buys little here, because the
instrument-UUID cache and the quote cache already keep call volume low. Revisit
only if measurements show the handshake mattering.
"""
import asyncio
import json
import threading

from src.services.broker_limits import RateLimitExceeded, consume
from src.services.robinhood_auth import MCP_URL, have_credentials

_loop = None
_loop_lock = threading.Lock()


class MCPCallFailed(Exception):
    """A tool call did not return usable data. Never retry this blindly."""


def _background_loop():
    """Starts (once) a daemon event loop for the SDK's async client."""
    global _loop
    with _loop_lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=run, name="robinhood-mcp", daemon=True).start()
        _loop = loop
        return _loop


async def _call_async(tool_name, arguments):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from src.services.robinhood_auth import build_oauth_provider

    auth = build_oauth_provider()
    async with streamablehttp_client(MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, arguments)


def call_tool(tool_name, arguments, max_calls_per_min=None, timeout=45):
    """Calls an MCP tool and returns the decoded JSON payload.

    Raises MCPCallFailed on any problem - including a refused rate-limit
    reservation, so exhausting the budget can never be mistaken for a bad quote.
    """
    if not have_credentials():
        raise MCPCallFailed(
            "not authenticated to Robinhood; run scripts/robinhood_login.py"
        )

    try:
        consume(max_per_min=max_calls_per_min, endpoint=tool_name) if max_calls_per_min \
            else consume(endpoint=tool_name)
    except RateLimitExceeded as e:
        raise MCPCallFailed(str(e))

    _log_call(tool_name)

    loop = _background_loop()
    future = asyncio.run_coroutine_threadsafe(_call_async(tool_name, arguments), loop)
    try:
        result = future.result(timeout=timeout)
    except Exception as e:
        raise MCPCallFailed(f"{tool_name} failed: {type(e).__name__}: {e}")

    if getattr(result, "isError", False):
        raise MCPCallFailed(f"{tool_name} returned an error: {_text_of(result)}")

    payload = _text_of(result)
    if not payload:
        raise MCPCallFailed(f"{tool_name} returned no content")
    try:
        return json.loads(payload)
    except ValueError:
        raise MCPCallFailed(f"{tool_name} returned non-JSON content: {payload[:200]}")


def _text_of(result):
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _log_call(endpoint):
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


def account_is_tradeable(account_number):
    """Checks the preconditions the tool docs require before any order.

    `place_option_order` and `review_option_order` both state: the account must
    be agentic_allowed=true and hold option_level_2 or option_level_3, and must
    NOT be called otherwise. Enforcing that here means a misconfigured account
    fails as a clean skip instead of a rejected order.

    Returns (ok, reason).
    """
    try:
        data = call_tool("get_accounts", {})
    except MCPCallFailed as e:
        return False, str(e)

    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return False, "get_accounts returned an unexpected shape"

    for account in accounts:
        if str(account.get("account_number")) != str(account_number):
            continue
        if not account.get("agentic_allowed"):
            return False, f"account {account_number} is not agentic_allowed"
        level = (account.get("option_level") or "").lower()
        if level not in ("option_level_2", "option_level_3"):
            return False, (
                f"account {account_number} has option level '{level or 'none'}'; "
                "level 2 or 3 is required for single-leg options"
            )
        return True, "ok"

    return False, f"account {account_number} not found in this Robinhood login"
