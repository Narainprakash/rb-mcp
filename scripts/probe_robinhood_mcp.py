#!/usr/bin/env python
"""
Read-only discovery probe for the Robinhood Agentic Trading MCP.

Answers the three questions blocking the trade-routing provider:

  1. What tools does the server expose, and with what argument schemas?
  2. Does it support OPTIONS, or equities only? (specs.md section 0.2 flags
     this as unresolved - at beta launch Agentic Trading was equities-only.)
  3. Where did the Hermes Agent cache its OAuth token, and can this process
     reuse it?

SAFETY: this script only reads configuration and calls `tools/list`. It never
invokes a tool, so it cannot place, modify, or cancel an order. Token values
are never printed - only the paths they were found at.

    venv/bin/python scripts/probe_robinhood_mcp.py
"""
import asyncio
import json
import os
import sys

HERMES_DIR = os.path.expanduser("~/.hermes")
AGENT_CONFIG = os.path.join(HERMES_DIR, "config.yaml")

# Tool names containing any of these suggest options support.
OPTION_HINTS = ("option", "contract", "strike", "expiry", "expiration", "put", "call")
# Tool names containing any of these place or change orders - listed so the
# report can flag them, never so they can be called from here.
ORDER_HINTS = ("order", "buy", "sell", "trade", "place", "cancel")


# The tools the trading pipeline would actually use. Full schemas are dumped
# for these so the provider can be written against real argument names.
TOOLS_OF_INTEREST = (
    "get_option_quotes",
    "get_option_instruments",
    "get_option_chains",
    "review_option_order",
    "place_option_order",
    "cancel_option_order",
    "get_option_orders",
    "get_option_positions",
    "get_accounts",
)

AUTH_FILE = os.path.join(HERMES_DIR, "auth.json")


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _find_token(node, path=""):
    """Recursively locates an access token. Returns (key_path, value).

    Reports where the token lives, so the provider knows how to read it, while
    the caller stays responsible for never printing the value itself.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if isinstance(value, str) and value:
                lowered = key.lower()
                if lowered in ("access_token", "accesstoken", "token", "bearer"):
                    return here, value
            found = _find_token(value, here)
            if found[1]:
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found = _find_token(item, f"{path}[{index}]")
            if found[1]:
                return found
    return "", None


def load_agent_token():
    section("2b. OAuth token reuse")
    if not os.path.exists(AUTH_FILE):
        print(f"  {AUTH_FILE} not found.")
        return None

    try:
        with open(AUTH_FILE) as handle:
            data = json.load(handle)
    except Exception as e:
        print(f"  Could not parse {AUTH_FILE}: {e}")
        return None

    def describe(node, indent="    "):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    print(f"{indent}{key}:")
                    describe(value, indent + "  ")
                else:
                    kind = type(value).__name__
                    size = len(value) if isinstance(value, str) else value
                    shown = f"<{kind}, {size} chars>" if isinstance(value, str) else f"<{kind}: {size}>"
                    print(f"{indent}{key}: {shown}")

    print(f"  Structure of {AUTH_FILE} (keys and value sizes only, no values):")
    describe(data)

    key_path, token = _find_token(data)
    if token:
        print(f"\n  Token found at: {key_path}  ({len(token)} chars)")
        print("  This process can read it - same user, mode 0600.")
    else:
        print("\n  No obvious access token key. The provider will need the exact path;")
        print("  inspect the structure above to identify it.")
    return token


def find_agent_config():
    section("1. Hermes Agent MCP configuration")
    if not os.path.exists(AGENT_CONFIG):
        print(f"  NOT FOUND: {AGENT_CONFIG}")
        return None
    try:
        import yaml
    except ImportError:
        print("  PyYAML not installed in this venv.")
        return None

    with open(AGENT_CONFIG) as handle:
        config = yaml.safe_load(handle) or {}

    servers = config.get("mcp_servers") or {}
    print(f"  Config: {AGENT_CONFIG}")
    print(f"  Servers registered: {', '.join(servers) or '(none)'}")

    robinhood = servers.get("robinhood")
    if not robinhood:
        print("  No 'robinhood' entry. Add it per README section 4.2.")
        return None

    # Print the entry but redact anything that looks like a secret.
    safe = {
        k: ("<redacted>" if any(s in k.lower() for s in ("token", "secret", "key", "password")) else v)
        for k, v in robinhood.items()
    }
    print(f"  robinhood entry: {json.dumps(safe, default=str)}")
    return robinhood


def find_cached_credentials():
    section("2. Cached OAuth credentials")
    if not os.path.isdir(HERMES_DIR):
        print(f"  {HERMES_DIR} does not exist.")
        return []

    candidates = []
    for root, dirs, files in os.walk(HERMES_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git")]
        for name in files:
            lowered = name.lower()
            if any(hint in lowered for hint in ("token", "cred", "auth", "oauth", "session")):
                path = os.path.join(root, name)
                try:
                    size = os.path.getsize(path)
                    mode = oct(os.stat(path).st_mode & 0o777)
                except OSError:
                    size, mode = "?", "?"
                candidates.append(path)
                print(f"  {path}  ({size} bytes, mode {mode})")

    if not candidates:
        print("  No obvious credential files found by name.")
        print("  The token may live inside a database or a differently named file.")
        print(f"  Worth inspecting manually: ls -la {HERMES_DIR}")
    else:
        print("\n  (Contents deliberately not printed. We only needed the location.)")
    return candidates


async def probe_tools(url, token=None):
    section("3. Tools exposed by the Robinhood MCP")
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        print("  The 'mcp' package is not installed. To run the live probe:")
        print("      venv/bin/pip install mcp")
        print("  Re-run this script afterwards.")
        return None

    headers = {"Authorization": f"Bearer {token}"} if token else None
    if not token:
        print("  No token supplied, attempting unauthenticated connection.")
        print("  A 401 here is expected and still informative - it confirms the")
        print("  endpoint is reachable and that auth is the only missing piece.")

    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    except Exception as e:
        print(f"  Connection failed: {type(e).__name__}: {e}")
        return None

    tools = result.tools
    print(f"  Connected. {len(tools)} tool(s) exposed.")

    by_name = {t.name: t for t in tools}
    print(f"\n  Full schemas for the {len(TOOLS_OF_INTEREST)} tools the pipeline needs:")
    for name in TOOLS_OF_INTEREST:
        tool = by_name.get(name)
        print(f"\n  --- {name} ---")
        if not tool:
            print("      NOT EXPOSED by this server")
            continue
        if tool.description:
            print(f"      {tool.description.strip()}")
        schema = getattr(tool, "inputSchema", None)
        if schema:
            print("      inputSchema:")
            for line in json.dumps(schema, indent=2).splitlines():
                print(f"        {line}")

    names = " ".join(t.name.lower() + " " + (t.description or "").lower() for t in tools)
    section("4. Verdict")
    options = [h for h in OPTION_HINTS if h in names]
    orders = [t.name for t in tools if any(h in t.name.lower() for h in ORDER_HINTS)]
    print(f"  Options support: {'LIKELY - matched ' + ', '.join(options) if options else 'NOT DETECTED (equities only?)'}")
    print(f"  Order-placing tools: {', '.join(orders) if orders else 'none detected'}")
    if not options:
        print("\n  If options are genuinely unsupported, the options flow must stay")
        print("  in paper mode regardless of everything else (specs.md 0.2).")
    return tools


def main():
    print("Robinhood MCP discovery probe (read-only; never invokes a tool)")

    entry = find_agent_config()
    find_cached_credentials()
    token = os.environ.get("ROBINHOOD_MCP_TOKEN") or load_agent_token()

    url = None
    if entry:
        url = entry.get("url")
    if not url:
        url = "https://agent.robinhood.com/mcp/trading"
        print(f"\n  Falling back to the documented URL: {url}")

    asyncio.run(probe_tools(url, token))

    section("Next step")
    print("  Paste this output back. With the tool names and argument schemas")
    print("  the provider can be written against reality instead of guesses.")
    print("  Do NOT paste any token values - only these findings are needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
