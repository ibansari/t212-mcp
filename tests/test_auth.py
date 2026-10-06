import httpx
import pytest
from fastmcp.server.auth.providers.workos import AuthKitProvider

from t212_mcp.auth import ConfigError, authkit_auth, http_auth
from t212_mcp.config import Settings

AUTHKIT = "https://example-app.authkit.app"


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    for var in ("T212_MCP_AUTH_TOKEN", "T212_AUTHKIT_DOMAIN", "T212_PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(var, raising=False)


def settings(**kw):
    return Settings(_env_file=None, api_key="k", **kw)


def test_authkit_when_configured_else_bearer_else_none():
    auth = http_auth(settings(authkit_domain=AUTHKIT, public_url="https://mcp.example.com", mcp_auth_token="tok"))
    assert isinstance(auth, AuthKitProvider)
    assert http_auth(settings(mcp_auth_token="tok")) is not None and not isinstance(http_auth(settings(mcp_auth_token="tok")), AuthKitProvider)
    assert http_auth(settings()) is None


def test_authkit_needs_a_public_url():
    with pytest.raises(ConfigError, match="T212_PUBLIC_URL"):
        authkit_auth(settings(authkit_domain=AUTHKIT))


def test_public_url_defaults_to_railway_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "t212.up.railway.app")
    assert isinstance(http_auth(settings(authkit_domain=AUTHKIT)), AuthKitProvider)


async def test_http_app_points_clients_at_authkit_and_requires_a_token(monkeypatch):
    from t212_mcp import server

    monkeypatch.setattr(server.mcp, "auth", authkit_auth(settings(authkit_domain=AUTHKIT, public_url="https://mcp.example.com")))
    app = server.mcp.http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com") as c:
        meta = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert meta["authorization_servers"] == [f"{AUTHKIT}/"] or meta["authorization_servers"] == [AUTHKIT]
        r = await c.post("/mcp", json={})
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
        assert (await c.get("/health")).text == "ok"


def test_blank_env_values_count_as_unset():
    s = settings(api_secret="", mcp_auth_token="", authkit_domain=" ", workos_api_key="")
    assert s.api_secret is None and s.mcp_auth_token is None and s.authkit_domain is None and s.workos_api_key is None
    assert http_auth(s) is None  # so a public --http refuses to start
