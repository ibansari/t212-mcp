"""Authentication for the HTTP transport: GitHub sign-in limited to allowed accounts, or a static bearer token."""

import hmac
import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.postgresql import PostgreSQLStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from sqlalchemy.engine import make_url

from .config import Settings

log = logging.getLogger("t212_mcp.auth")


class ConfigError(Exception):
    pass


def bearer_auth(token: str) -> DebugTokenVerifier:
    """Accept exactly one static bearer token, compared in constant time."""
    return DebugTokenVerifier(validate=lambda t: hmac.compare_digest(t.encode(), token.encode()), client_id="t212-mcp")


class AllowlistGitHubProvider(GitHubProvider):
    """GitHub sign-in that only lets allow-listed accounts through.

    Entries are GitHub usernames (case-insensitive) or numeric user IDs; an ID keeps working if the account is
    renamed. Anyone with a GitHub account can complete the sign-in flow, but every request re-checks the account
    behind the token, so other accounts get 401 on every call.
    """

    def __init__(self, *, allowed_users: set[str], **kwargs):
        if not allowed_users:
            raise ConfigError("allowed_users must not be empty")
        kwargs.setdefault("required_scopes", ["read:user"])  # identity only; FastMCP's default "user" can write
        super().__init__(**kwargs)
        self.allowed_users = {u.strip().lower() for u in allowed_users}

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        login = str(claims.get("login") or "").lower()
        user_id = str(claims.get("sub") or "")
        if (login and login in self.allowed_users) or (user_id and user_id in self.allowed_users):
            return access
        log.warning("Rejected GitHub account %s (id %s): not in T212_ALLOWED_GITHUB_USERS", login or "?", user_id or "?")
        return None


def oauth_storage(database_url: str, secret: str) -> FernetEncryptionWrapper:
    """Client registrations and upstream tokens, encrypted, in Postgres, so sign-ins survive redeploys."""
    url = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    key = derive_jwt_key(high_entropy_material=secret, salt="t212-mcp-oauth-storage")
    return FernetEncryptionWrapper(
        key_value=PostgreSQLStore(url=url, table_name="oauth_kv"),
        fernet=Fernet(key=key),
        raise_on_decryption_error=False,  # a rotated secret just means signing in again
    )


def github_auth(settings: Settings) -> AllowlistGitHubProvider:
    missing = [name for name, value in (("T212_GITHUB_CLIENT_SECRET", settings.github_client_secret),
                                        ("T212_ALLOWED_GITHUB_USERS", settings.allowed_github_user_set),
                                        ("T212_PUBLIC_URL", settings.public_base_url)) if not value]
    if missing:
        raise ConfigError(f"GitHub sign-in needs {', '.join(missing)}")
    secret = settings.github_client_secret.get_secret_value()
    return AllowlistGitHubProvider(
        allowed_users=settings.allowed_github_user_set,
        client_id=settings.github_client_id,
        client_secret=secret,
        base_url=settings.public_base_url,
        client_storage=oauth_storage(settings.database_url, secret),
    )


def http_auth(settings: Settings) -> AuthProvider | None:
    """GitHub sign-in when configured, else the static bearer token, else none (localhost only)."""
    if settings.github_client_id:
        if settings.mcp_auth_token is not None:
            log.warning("T212_MCP_AUTH_TOKEN is ignored because GitHub sign-in is configured")
        return github_auth(settings)
    if settings.mcp_auth_token is not None:
        return bearer_auth(settings.mcp_auth_token.get_secret_value())
    return None
