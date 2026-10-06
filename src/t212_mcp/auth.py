"""Authentication for the HTTP transport: Google sign-in limited to allowed accounts, or a static bearer token."""

import hmac
import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.auth.providers.google import GoogleProvider
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


class AllowlistGoogleProvider(GoogleProvider):
    """Google sign-in that only lets verified, allow-listed email addresses through.

    Anyone with a Google account can complete the sign-in flow, but every request re-checks the account behind the
    token, so other accounts get 401 on every call.
    """

    def __init__(self, *, allowed_emails: set[str], **kwargs):
        if not allowed_emails:
            raise ConfigError("allowed_emails must not be empty")
        kwargs.setdefault("required_scopes", ["openid", "email"])
        super().__init__(**kwargs)
        self.allowed_emails = {e.strip().lower() for e in allowed_emails}

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        email = str(claims.get("email") or "").lower()
        if claims.get("email_verified") in (True, "true") and email in self.allowed_emails:
            return access
        log.warning("Rejected Google account %s: not in T212_ALLOWED_EMAILS", email or "<no email>")
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


def google_auth(settings: Settings) -> AllowlistGoogleProvider:
    missing = [name for name, value in (("T212_GOOGLE_CLIENT_SECRET", settings.google_client_secret),
                                        ("T212_ALLOWED_EMAILS", settings.allowed_email_set),
                                        ("T212_PUBLIC_URL", settings.public_base_url)) if not value]
    if missing:
        raise ConfigError(f"Google sign-in needs {', '.join(missing)}")
    secret = settings.google_client_secret.get_secret_value()
    return AllowlistGoogleProvider(
        allowed_emails=settings.allowed_email_set,
        client_id=settings.google_client_id,
        client_secret=secret,
        base_url=settings.public_base_url,
        client_storage=oauth_storage(settings.database_url, secret),
    )


def http_auth(settings: Settings) -> AuthProvider | None:
    """Google sign-in when configured, else the static bearer token, else none (localhost only)."""
    if settings.google_client_id:
        if settings.mcp_auth_token is not None:
            log.warning("T212_MCP_AUTH_TOKEN is ignored because Google sign-in is configured")
        return google_auth(settings)
    if settings.mcp_auth_token is not None:
        return bearer_auth(settings.mcp_auth_token.get_secret_value())
    return None
