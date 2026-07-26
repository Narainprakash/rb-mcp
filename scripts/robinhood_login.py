#!/usr/bin/env python
"""
One-time OAuth login for the trading process.

Authorizes this bot against the Robinhood Agentic Trading MCP and caches the
resulting tokens. The refresh_token grant means you should only need to do this
once - restarts reuse the stored credentials.

    venv/bin/python scripts/robinhood_login.py

Designed for a headless VPS: it prints an authorization URL, you open it in a
desktop browser, approve, and paste back the URL your browser was redirected to
(which will show a connection error - that is expected, nothing is listening on
the redirect port).

After logging in, this lists the server's tools as a connectivity check. It
never places an order.
"""
import asyncio
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.db import get_connection, init_db  # noqa: E402
from src.services.robinhood_auth import (  # noqa: E402
    MCP_URL,
    REDIRECT_URI,
    build_oauth_provider,
    migrate_legacy_token,
    token_path,
)


def resolve_user(username):
    """Maps a username to its id, listing the options when it is ambiguous.

    Credentials are per user because tenants are different people with their
    own Robinhood logins - so this refuses to guess rather than authorizing
    someone against the wrong account.
    """
    init_db()
    conn = get_connection()
    try:
        users = conn.execute(
            "SELECT id, username FROM users WHERE is_active = 1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    if not users:
        raise SystemExit(
            "No users exist yet. Create one first:\n"
            "  venv/bin/python scripts/add_user.py"
        )

    if username:
        for user in users:
            if user["username"] == username:
                return user["id"], user["username"]
        names = ", ".join(u["username"] for u in users)
        raise SystemExit(f"No active user named '{username}'. Known users: {names}")

    if len(users) == 1:
        return users[0]["id"], users[0]["username"]

    names = "\n".join(f"  --user {u['username']}" for u in users)
    raise SystemExit(
        "Several users exist, so the login target is ambiguous. Re-run with one of:\n"
        + names
    )


async def show_authorization_url(url: str) -> None:
    print("\n" + "=" * 70)
    print("STEP 1 - Authorize in a desktop browser")
    print("=" * 70)
    print("\nOpen this URL:\n")
    print(f"  {url}\n")
    print("Approve access for your AGENTIC account (not your main brokerage).")


async def read_redirect_url() -> tuple:
    print("=" * 70)
    print("STEP 2 - Paste the redirect URL")
    print("=" * 70)
    print(f"\nYour browser will fail to load {REDIRECT_URI} - that is expected,")
    print("nothing is listening there. Copy the full URL from the address bar")
    print("(it contains ?code=...) and paste it below.\n")

    raw = input("Redirect URL: ").strip()
    if not raw:
        raise SystemExit("No URL entered; aborting.")

    query = parse_qs(urlparse(raw).query)
    if "error" in query:
        raise SystemExit(f"Authorization was denied: {query['error'][0]}")
    code = query.get("code", [None])[0]
    if not code:
        raise SystemExit("That URL has no ?code= parameter. Copy the whole address bar.")
    return code, query.get("state", [None])[0]


async def main(username=None) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    migrate_legacy_token()
    user_id, resolved_name = resolve_user(username)

    print("Robinhood MCP login")
    print(f"  Server: {MCP_URL}")
    print(f"  Authorizing as dashboard user: {resolved_name} (id {user_id})")
    print(f"  Tokens will be written to: {token_path(user_id)} (mode 0600)")
    print("  This login belongs to this user alone - other users authorize separately.")

    auth = build_oauth_provider(
        user_id,
        redirect_handler=show_authorization_url,
        callback_handler=read_redirect_url,
    )

    try:
        async with streamablehttp_client(MCP_URL, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
    except Exception as e:
        print(f"\nLogin failed: {type(e).__name__}: {e}")
        for sub in getattr(e, "exceptions", []) or []:
            print(f"  caused by {type(sub).__name__}: {sub}")
        print("\nIf this was a redirect_uri mismatch, the authorization server")
        print(f"may not accept {REDIRECT_URI}. Report the error and we can adjust it.")
        return 1

    print("\n" + "=" * 70)
    print(f"SUCCESS - authenticated, {len(tools)} tools available")
    print("=" * 70)

    names = sorted(t.name for t in tools)
    options = [n for n in names if "option" in n]
    print(f"\nOptions-related tools ({len(options)}):")
    for name in options:
        print(f"  {name}")

    print(f"\nCredentials cached. Run this to dump the full argument schemas:")
    print("  venv/bin/python scripts/probe_robinhood_mcp.py")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Authorize a dashboard user against Robinhood.")
    parser.add_argument("--user", help="Dashboard username. Optional when only one user exists.")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(main(args.user)))
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
