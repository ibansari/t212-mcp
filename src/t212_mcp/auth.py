"""Authentication for the HTTP transport: WorkOS AuthKit (multi-user), or a static bearer token (single-user)."""

import hmac
import logging

from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider

from .config import Settings

log = logging.getLogger("t212_mcp.auth")


class ConfigError(Exception):
    pass


def bearer_auth(token: str) -> DebugTokenVerifier:
    """Accept exactly one static bearer token, compared in constant time."""
    return DebugTokenVerifier(validate=lambda t: hmac.compare_digest(t.encode(), token.encode()), client_id="t212-mcp")


def authkit_auth(settings: Settings) -> AuthKitProvider:
    """MCP clients sign in with WorkOS AuthKit (hosted login, dynamic client registration); this server only
    verifies the AuthKit-issued JWTs. Who may sign in is configured in the WorkOS dashboard."""
    if not settings.public_base_url:
        raise ConfigError("AuthKit sign-in needs T212_PUBLIC_URL")
    return AuthKitProvider(authkit_domain=settings.authkit_domain, base_url=settings.public_base_url)


def http_auth(settings: Settings) -> AuthProvider | None:
    """AuthKit when configured, else the static bearer token, else none (localhost only)."""
    if settings.authkit_domain:
        if settings.mcp_auth_token is not None:
            log.warning("T212_MCP_AUTH_TOKEN is ignored because AuthKit sign-in is configured")
        return authkit_auth(settings)
    if settings.mcp_auth_token is not None:
        return bearer_auth(settings.mcp_auth_token.get_secret_value())
    return None
