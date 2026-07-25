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


def _flatten_exception(error, depth=0):
    """Yields the real causes inside an ExceptionGroup / __cause__ chain."""
    if depth > 5:
        return
    for sub in getattr(error, "exceptions", []) or []:
        yield sub
        yield from _flatten_exception(sub, depth + 1)
    cause = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
    if cause is not None and cause is not error:
        yield cause
        yield from _flatten_exception(cause, depth + 1)


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
    """Full inventory of ~/.hermes.

    Searching by filename keyword was too narrow - it surfaced auth.json (which
    turned out to hold OpenRouter credentials, not MCP tokens) while telling us
    nothing about where the Robinhood OAuth token actually lives. List
    everything instead, minus the WhatsApp session noise.
    """
    section("2. Hermes state directory inventory")
    if not os.path.isdir(HERMES_DIR):
        print(f"  {HERMES_DIR} does not exist.")
        return []

    found = []
    for root, dirs, files in os.walk(HERMES_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git")]
        # The WhatsApp bridge keeps dozens of session files; they are unrelated
        # to MCP auth and drown out everything else.
        if "whatsapp" in root.replace("\\", "/").split("/"):
            continue
        rel_root = os.path.relpath(root, HERMES_DIR)
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
                mode = oct(os.stat(path).st_mode & 0o777)
            except OSError:
                size, mode = "?", "?"
            display = name if rel_root == "." else os.path.join(rel_root, name)
            print(f"  {display}  ({size} bytes, mode {mode})")
            found.append(path)

    print("\n  (Contents not printed. Looking for where an MCP OAuth token lives.)")
    return found


def inspect_json_structure(path, label):
    """Prints a JSON file's shape - keys and value sizes, never values."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except Exception as e:
        print(f"  {label}: not readable as JSON ({e})")
        return None

    print(f"  {label} structure:")

    def describe(node, indent="    "):
        if isinstance(node, dict):
            if not node:
                print(f"{indent}(empty)")
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    print(f"{indent}{key}:")
                    describe(value, indent + "  ")
                else:
                    kind = type(value).__name__
                    shown = f"<{kind}, {len(value)} chars>" if isinstance(value, str) else f"<{kind}: {value}>"
                    print(f"{indent}{key}: {shown}")
        elif isinstance(node, list):
            print(f"{indent}[{len(node)} item(s)]")
            if node:
                describe(node[0], indent + "  ")

    describe(data)
    return data


def discover_oauth(url):
    """Asks the server how to authenticate, without any credentials.

    If the agent's token cannot be reused, this process can run its own OAuth
    2.1 PKCE flow - which is cleaner anyway, since it depends on the published
    protocol rather than on Hermes' internal storage layout.
    """
    section("3. OAuth discovery (unauthenticated, read-only)")
    try:
        import requests
    except ImportError:
        print("  requests not available.")
        return

    from urllib.parse import urlparse
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # An MCP server should answer 401 with a WWW-Authenticate header pointing at
    # its protected-resource metadata (MCP authorization spec).
    try:
        response = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=15,
        )
        print(f"  POST {url} -> HTTP {response.status_code}")
        for header in ("www-authenticate", "WWW-Authenticate"):
            if header in response.headers:
                print(f"    WWW-Authenticate: {response.headers[header]}")
        if response.status_code == 401:
            print("    (401 is the expected, useful answer: auth is the only gap.)")
    except Exception as e:
        print(f"  POST failed: {type(e).__name__}: {e}")

    for suffix in ("/.well-known/oauth-protected-resource",
                   "/.well-known/oauth-authorization-server",
                   "/.well-known/openid-configuration"):
        try:
            response = requests.get(origin + suffix, timeout=15)
            print(f"  GET {suffix} -> HTTP {response.status_code}")
            if response.status_code == 200:
                try:
                    body = response.json()
                    for key in ("issuer", "authorization_endpoint", "token_endpoint",
                                "registration_endpoint", "resource", "scopes_supported",
                                "authorization_servers", "code_challenge_methods_supported"):
                        if key in body:
                            print(f"    {key}: {json.dumps(body[key])}")
                except ValueError:
                    print("    (200 but not JSON)")
        except Exception as e:
            print(f"  GET {suffix} failed: {type(e).__name__}: {e}")


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

    # Prefer this process's own OAuth credentials over a scavenged token.
    auth = None
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        from src.services.robinhood_auth import build_oauth_provider, have_credentials
        if have_credentials():
            print("  Using stored OAuth credentials (scripts/robinhood_login.py).")
            auth, headers = build_oauth_provider(), None
        elif not token:
            print("  Not logged in yet. Run this first:")
            print("      venv/bin/python scripts/robinhood_login.py")
            print("  Continuing unauthenticated - expect a 401.")
    except ImportError:
        pass

    try:
        async with streamablehttp_client(url, headers=headers, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    except Exception as e:
        # The MCP client runs inside a TaskGroup, so the real cause arrives
        # wrapped in an ExceptionGroup. Printing only the wrapper says nothing.
        print(f"  Connection failed: {type(e).__name__}: {e}")
        for depth, cause in enumerate(_flatten_exception(e), start=1):
            print(f"    [{depth}] {type(cause).__name__}: {cause}")
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
    if not token:
        # auth.json holds OpenRouter credentials, not MCP tokens. Check the
        # other plausible store before concluding the token is not on disk.
        section("2c. Other candidate token stores")
        inspect_json_structure(os.path.join(HERMES_DIR, "sessions", "sessions.json"),
                               "sessions/sessions.json")

    url = None
    if entry:
        url = entry.get("url")
    if not url:
        url = "https://agent.robinhood.com/mcp/trading"
        print(f"\n  Falling back to the documented URL: {url}")

    discover_oauth(url)
    asyncio.run(probe_tools(url, token))

    section("Next step")
    print("  Paste this output back. With the tool names and argument schemas")
    print("  the provider can be written against reality instead of guesses.")
    print("  Do NOT paste any token values - only these findings are needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
