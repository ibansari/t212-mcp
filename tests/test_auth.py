import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.memory import MemoryStore

from t212_mcp.auth import AllowlistGitHubProvider, ConfigError, github_auth, http_auth
from t212_mcp.config import Settings


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    for var in ("T212_MCP_AUTH_TOKEN", "T212_GITHUB_CLIENT_ID", "T212_GITHUB_CLIENT_SECRET", "T212_ALLOWED_GITHUB_USERS",
                "T212_PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(var, raising=False)


def provider(**kw):
    return AllowlistGitHubProvider(allowed_users={"IBansari", "424242"}, client_id="Ov23liexample",
                                   client_secret="secret", base_url="https://mcp.example.com",
                                   client_storage=MemoryStore(), **kw)


@pytest.mark.parametrize("claims,allowed", [
    ({"login": "ibansari", "sub": "1"}, True),
    ({"login": "IBANSARI", "sub": "1"}, True),
    ({"login": "renamed-me", "sub": "424242"}, True),  # allowed by numeric ID
    ({"login": "someone-else", "sub": "2"}, False),
    ({}, False),
])
async def test_only_allowlisted_accounts_get_in(monkeypatch, claims, allowed):
    async def upstream(self, token):
        return AccessToken(token=token, client_id="c", scopes=["read:user"], claims=claims)

    monkeypatch.setattr(GitHubProvider, "load_access_token", upstream)
    result = await provider().load_access_token("tok")
    assert (result is not None) is allowed


async def test_invalid_upstream_token_stays_rejected(monkeypatch):
    async def upstream(self, token):
        return None

    monkeypatch.setattr(GitHubProvider, "load_access_token", upstream)
    assert await provider().load_access_token("tok") is None


def test_requests_read_only_scope():
    assert provider().required_scopes == ["read:user"]


def test_github_auth_requires_allowlist_and_public_url():
    s = Settings(_env_file=None, api_key="k", github_client_id="id", github_client_secret="secret")
    with pytest.raises(ConfigError, match="T212_ALLOWED_GITHUB_USERS, T212_PUBLIC_URL"):
        github_auth(s)


def test_public_url_defaults_to_railway_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "t212.up.railway.app")
    s = Settings(_env_file=None, api_key="k", github_client_id="id", github_client_secret="secret",
                 allowed_github_users="@IBansari, 424242", database_url="postgresql+psycopg://u:p@localhost/db")
    assert s.public_base_url == "https://t212.up.railway.app"
    auth = http_auth(s)
    assert isinstance(auth, AllowlistGitHubProvider) and auth.allowed_users == {"ibansari", "424242"}


def test_bearer_token_when_github_not_configured():
    s = Settings(_env_file=None, api_key="k", mcp_auth_token="tok")
    assert not isinstance(http_auth(s), AllowlistGitHubProvider) and http_auth(s) is not None
    assert http_auth(Settings(_env_file=None, api_key="k")) is None


async def test_http_app_publishes_oauth_metadata_and_requires_sign_in(monkeypatch):
    import httpx

    from t212_mcp import server

    monkeypatch.setattr(server.mcp, "auth", provider())
    app = server.mcp.http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com") as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["authorization_endpoint"] == "https://mcp.example.com/authorize"
        r = await c.post("/mcp", json={})
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
        assert (await c.get("/health")).text == "ok"


def test_blank_env_values_count_as_unset():
    s = Settings(_env_file=None, api_key="k", api_secret="", mcp_auth_token="", github_client_id=" ")
    assert s.api_secret is None and s.mcp_auth_token is None and s.github_client_id is None
    assert http_auth(s) is None  # so a public --http refuses to start
