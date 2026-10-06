"""Multi-user: identity, Vault-stored keys, per-user isolation, privacy rules, the upgrade, and the account page."""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from sqlalchemy import text

from t212_mcp import account_page, accounts, db, server, snapshots
from t212_mcp.client import Credentials, T212Client
from t212_mcp.config import Settings
from t212_mcp.lookthrough.store import Store

BASE = "https://demo.trading212.com/api/v0"
SNAP = {"taken_at": "2026-10-06T07:00:00+00:00", "total_value": 100.0, "invested_value": 100.0, "unrealized_pnl": 0.0,
        "cash_available": 0.0, "positions": {}}


class FakeVault:
    def __init__(self):
        self.objects, self.n = {}, 0

    async def create_kv(self, *, name, value, key_context):
        self.n += 1
        self.objects[f"obj_{self.n}"] = (name, value)
        return SimpleNamespace(id=f"obj_{self.n}")

    async def update_kv(self, object_id, *, value):
        self.objects[object_id] = (self.objects[object_id][0], value)

    async def get_kv(self, object_id):
        return SimpleNamespace(value=self.objects[object_id][1])

    async def delete_kv(self, object_id):
        self.objects.pop(object_id)


@pytest.fixture
def settings(database_url, monkeypatch):
    s = Settings(_env_file=None, api_key="owner-key", env="demo", database_url=database_url, owner_email="me@example.com",
                 admin_emails="admin@example.com", public_url="https://mcp.example.com",
                 authkit_domain="https://example-app.authkit.app", workos_api_key="sk_test", workos_client_id="client_x",
                 workos_cookie_password="Y7cR3Hc9vJ5sC1Gq0n2m8bW4tF6xK0pL9aD3eS5uQ1o=")
    vault = FakeVault()
    monkeypatch.setattr(accounts, "_settings", s)
    monkeypatch.setattr(accounts, "workos", lambda settings: SimpleNamespace(vault=vault))
    for cache in (accounts._clients, accounts._credentials, accounts._emails, accounts._last_seen):
        cache.clear()
    monkeypatch.setattr(server, "_client", None)
    return s


def as_caller(monkeypatch, sub: str | None, email: str | None = None):
    token = AccessToken(token="t", client_id="c", scopes=[], claims={"sub": sub}) if sub else None
    monkeypatch.setattr(accounts, "get_access_token", lambda: token)
    if sub:
        accounts._emails[sub] = email


# ---------------------------------------------------------------- identity


async def test_callers_map_to_owner_admins_and_users(settings, monkeypatch):
    as_caller(monkeypatch, None)
    assert (await accounts.current_user()).id == "owner"  # stdio / CLI
    as_caller(monkeypatch, "user_01OWNER", "Me@Example.com")
    me = await accounts.current_user()
    assert me.id == "owner" and me.is_owner and me.is_admin  # the owner signing in over HTTP keeps their data
    as_caller(monkeypatch, "user_01ADMIN", "admin@example.com")
    assert (await accounts.current_user()).is_admin
    as_caller(monkeypatch, "user_01FRIEND", "friend@example.com")
    friend = await accounts.current_user()
    assert friend.id == "user_01FRIEND" and not friend.is_admin and not friend.is_owner


# ---------------------------------------------------------------- keys in Vault


async def test_keys_live_in_vault_not_the_database(settings):
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)
    assert await accounts.client_for(friend) is None
    with pytest.raises(ToolError, match="https://mcp.example.com/account"):
        await accounts.require_client(friend)

    await accounts.save_credentials(settings, friend, Credentials("friend-key", "friend-secret", "demo"))
    with db.sessions(settings.database_url)() as s:
        stored = s.execute(text("SELECT * FROM user_credentials")).mappings().one()
    assert "friend-key" not in json.dumps({k: str(v) for k, v in stored.items()})  # only a Vault reference
    c = await accounts.client_for(friend)
    assert c.creds == Credentials("friend-key", "friend-secret", "demo")
    assert accounts.connection(settings, friend.id)["env"] == "demo"

    await accounts.delete_credentials(settings, friend.id)
    assert await accounts.client_for(friend) is None


# ---------------------------------------------------------------- isolation


def test_personal_data_never_crosses_users(settings):
    store = Store(settings.database_url)
    snapshots.save(settings.database_url, "demo", {**SNAP, "total_value": 1.0}, user_id="owner")
    snapshots.save(settings.database_url, "demo", {**SNAP, "total_value": 2.0}, user_id="user_01FRIEND")
    assert snapshots.load_latest(settings.database_url, "demo", user_id="owner")["total_value"] == 1.0
    assert snapshots.load_latest(settings.database_url, "demo", user_id="user_01FRIEND")["total_value"] == 2.0

    exposure = {"invested_value": 1, "securities_count": 0, "top10_pct": 0, "cash_inside_funds": 0,
                "coverage": {"funds_with_data_pct": None}}
    mine = store.save_exposure(exposure, user_id="owner", day=date(2026, 10, 6))
    store.save_exposure({**exposure, "invested_value": 2}, user_id="user_01FRIEND", day=date(2026, 10, 6))
    assert [d for d, _ in store.exposure_history(user_id="owner")] == ["2026-10-06"]
    with pytest.raises(LookupError):
        store.load_exposure(mine, user_id="user_01FRIEND")

    store.save_baseline(user_id="owner", env="demo", day=date(2026, 10, 6), taken_at=db.utcnow(), portfolio=SNAP, exposure=None)
    assert store.latest_baseline("demo", before=date(2026, 10, 7), user_id="user_01FRIEND") is None


async def test_deleting_my_data_keeps_everyone_elses(settings):
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)
    await accounts.save_credentials(settings, friend, Credentials("k", None, "demo"))
    snapshots.save(settings.database_url, "demo", SNAP, user_id=friend.id)
    snapshots.save(settings.database_url, "demo", SNAP, user_id="owner")
    await accounts.delete_user_data(settings, friend.id)
    assert snapshots.load_latest(settings.database_url, "demo", user_id=friend.id) is None
    assert snapshots.load_latest(settings.database_url, "demo", user_id="owner") is not None
    with pytest.raises(ValueError):
        await accounts.delete_user_data(settings, "owner")


# ---------------------------------------------------------------- tools


def position(ticker, isin, value):
    return {"instrument": {"ticker": ticker, "name": ticker, "isin": isin, "currency": "USD"}, "quantity": 1,
            "averagePricePaid": 1.0, "currentPrice": 1.0,
            "walletImpact": {"currentValue": value, "totalCost": value, "unrealizedProfitLoss": 0.0}}


@respx.mock
async def test_each_caller_sees_only_their_own_account(settings, monkeypatch):
    friend_auth = "Basic " + __import__("base64").b64encode(b"friend-key:friend-secret").decode()
    respx.get(f"{BASE}/equity/positions", headers={"Authorization": friend_auth}).respond(json=[position("FRND_US_EQ", "US0000000001", 50.0)])
    respx.get(f"{BASE}/equity/positions").respond(json=[position("OWNR_US_EQ", "US0000000002", 70.0)])
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)
    await accounts.save_credentials(settings, friend, Credentials("friend-key", "friend-secret", "demo"))

    async with Client(server.mcp) as c:
        as_caller(monkeypatch, "user_01FRIEND", "friend@example.com")
        mine = (await c.call_tool("get_positions", {})).data
        as_caller(monkeypatch, None)
        owners = (await c.call_tool("get_positions", {})).data
        as_caller(monkeypatch, "user_01STRANGER", "stranger@example.com")
        with pytest.raises(ToolError, match="Connect a read-only API key"):
            await c.call_tool("get_positions", {})
    assert [p["ticker"] for p in mine["positions"]] == ["FRND_US_EQ"]
    assert [p["ticker"] for p in owners["positions"]] == ["OWNR_US_EQ"]


@respx.mock
async def test_non_admins_get_no_agent_and_no_other_funds_traces(settings, monkeypatch):
    respx.get(f"{BASE}/equity/positions").respond(json=[])
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)
    await accounts.save_credentials(settings, friend, Credentials("friend-key", None, "demo"))
    started = {}

    async def fake_refresh(s, allow_agent=False, **kw):
        started.update(allow_agent=allow_agent, user_id=kw["user_id"])
        return {"as_of": "2026-10-06", "refresh_results": [], "coverage": {"funds_with_data_pct": None}}

    from t212_mcp.lookthrough import graph

    monkeypatch.setattr(graph, "refresh", fake_refresh)
    monkeypatch.setattr(server, "_refresh", {})
    monkeypatch.setattr(server, "_refresh_tasks", {})
    as_caller(monkeypatch, "user_01FRIEND", "friend@example.com")
    async with Client(server.mcp) as c:
        first = (await c.call_tool("refresh_etf_holdings", {"allow_agent": True})).data
        await server._refresh_tasks["user_01FRIEND"]
        again = (await c.call_tool("refresh_etf_holdings", {})).data
        with pytest.raises(ToolError, match="isn't one of your funds"):
            await c.call_tool("get_agent_trace", {"fund": "ENTITIES"})
    assert first["allow_agent"] is False and started == {"allow_agent": False, "user_id": "user_01FRIEND"}
    assert again["started"] is False and "try again" in again["reason"]  # cooldown


# ---------------------------------------------------------------- upgrade


def test_upgrade_turns_a_single_user_database_multi_user(settings):
    url = settings.database_url
    with db.engine(url).begin() as conn:  # make it look like yesterday's schema
        for table in ("portfolio_snapshots", "exposure_snapshots", "digest_baselines", "agent_runs"):
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN user_id"))
        conn.execute(text("ALTER TABLE exposure_snapshots ADD CONSTRAINT exposure_snapshots_as_of_key UNIQUE (as_of)"))
        conn.execute(text("CREATE TABLE oauth_kv (key text)"))
        conn.execute(text("INSERT INTO portfolio_snapshots (env, taken_at, total_value, invested_value, unrealized_pnl, "
                          "cash_available) VALUES ('live', now(), 1, 1, 0, 0)"))
    db.upgrade_multiuser(url)
    db.upgrade_multiuser(url)  # idempotent
    with db.engine(url).connect() as conn:
        assert conn.scalar(text("SELECT user_id FROM portfolio_snapshots")) == "owner"
        names = set(conn.scalars(text("SELECT conname FROM pg_constraint")))
        assert {"uq_exposure_snapshots_user_day", "uq_digest_baselines_user_env_day"} <= names
        assert "exposure_snapshots_as_of_key" not in names
        assert conn.scalar(text("SELECT to_regclass('oauth_kv')")) is None


# ---------------------------------------------------------------- account page


def page_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.mcp.http_app()), base_url="https://mcp.example.com")


async def test_sign_in_starts_at_authkit_with_a_state_cookie(settings, monkeypatch):
    monkeypatch.setattr(accounts, "workos", lambda s: SimpleNamespace(user_management=SimpleNamespace(
        get_authorization_url=lambda **kw: f"https://example-app.authkit.app/authorize?state={kw['state']}&redirect_uri={kw['redirect_uri']}")))
    async with page_client() as c:
        r = await c.get("/account/login")
    assert r.status_code == 302 and r.headers["location"].startswith("https://example-app.authkit.app/authorize")
    assert "redirect_uri=https://mcp.example.com/account/callback" in r.headers["location"]
    assert account_page.STATE_COOKIE in r.cookies


async def test_callback_rejects_a_missing_or_wrong_state(settings):
    async with page_client() as c:
        r = await c.get("/account/callback?code=abc&state=forged")
    assert "tampered" in r.text


@respx.mock
async def test_connecting_a_key_checks_it_and_stores_it(settings, monkeypatch):
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)

    async def signed_in(settings, request):
        return friend, None

    monkeypatch.setattr(account_page, "session_user", signed_in)
    respx.get(f"{BASE}/equity/account/summary").respond(json={"totalValue": 1})
    async with page_client() as c:
        cross_site = await c.post("/account/credentials", data={"api_key": "k", "env": "demo"},
                                  headers={"Origin": "https://evil.example.com"})
        ok = await c.post("/account/credentials", data={"api_key": "friend-key", "api_secret": "s", "env": "demo"},
                          headers={"Origin": "https://mcp.example.com"})
        page = await c.get("/account")
    assert "another site" in cross_site.text
    assert ok.status_code == 303 and "Connected" in ok.headers["location"]
    assert (await accounts.client_for(friend)).creds.api_key == "friend-key"
    assert "Connected: demo account" in page.text and "friend-key" not in page.text


@respx.mock
async def test_a_rejected_key_is_not_stored(settings, monkeypatch):
    friend = accounts.User("user_01FRIEND", "friend@example.com", False, False)

    async def signed_in(settings, request):
        return friend, None

    monkeypatch.setattr(account_page, "session_user", signed_in)
    respx.get(f"{BASE}/equity/account/summary").respond(401)
    async with page_client() as c:
        r = await c.post("/account/credentials", data={"api_key": "bad", "env": "demo"}, headers={"Origin": "https://mcp.example.com"})
    assert "accept that key" in r.text and accounts.connection(settings, friend.id) is None
