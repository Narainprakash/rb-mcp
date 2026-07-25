"""
OAuth 2.1 credentials for the Robinhood Agentic Trading MCP.

The trading process holds its **own** credentials rather than borrowing the
Hermes Agent's. That was the original plan, but the agent's `auth.json` turned
out to hold OpenRouter keys, not MCP tokens - and scavenging another process's
undocumented token store would have been fragile anyway: the agent owns refresh,
and an agent update could silently break us mid-session.

Discovery against the live server (2026-07-25) showed everything needed to be
self-sufficient:

    authorization_endpoint  https://robinhood.com/oauth
    token_endpoint          https://api.robinhood.com/oauth2/token/
    registration_endpoint   https://agent.robinhood.com/oauth/trading/register
    grant_types             authorization_code, refresh_token
    PKCE                    S256 (required)
    client auth             none (public client, no secret)
    scope                   internal

Dynamic Client Registration means no client ID has to be provisioned by hand,
and the refresh_token grant means the one-time login survives restarts.

Run `scripts/robinhood_login.py` once to authorize. Tokens are written 0600 and
the file is gitignored - it grants trading access to the agentic account.
"""
import json
import os

MCP_URL = "https://agent.robinhood.com/mcp/trading"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKEN_PATH = os.path.join(PROJECT_ROOT, ".robinhood_token.json")

# The authorization server rejects unregistered redirect URIs, and the dashboard
# already owns 8420. Nothing listens here: the headless flow has you paste the
# failed redirect URL back, exactly as the README describes for the agent.
REDIRECT_URI = "http://localhost:8421/callback"


def _write_private(path, payload):
    """Writes JSON readable only by the owner. These are trading credentials."""
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # not meaningful on Windows


class FileTokenStorage:
    """Persists tokens and the dynamically registered client to disk.

    The SDK's default storage is in-memory, which would mean a fresh browser
    authorization on every service restart - unusable for a bot that systemd
    restarts on failure.
    """

    def __init__(self, path=TOKEN_PATH):
        self.path = path

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path) as handle:
                return json.load(handle)
        except Exception:
            return {}

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens):
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        _write_private(self.path, data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        data = self._load().get("client")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        data = self._load()
        data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        _write_private(self.path, data)


def client_metadata():
    from mcp.shared.auth import OAuthClientMetadata
    return OAuthClientMetadata(
        client_name="Hermes Trading Bot",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="internal",
    )


def have_credentials():
    """Whether a login has been completed. Cheap enough for a status card."""
    data = FileTokenStorage()._load()
    return bool(data.get("tokens", {}).get("access_token"))


def build_oauth_provider(redirect_handler=None, callback_handler=None):
    from mcp.client.auth import OAuthClientProvider
    return OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=client_metadata(),
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
