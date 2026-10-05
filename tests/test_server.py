import base64
import time

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from t212_mcp import server, snapshots
from t212_mcp.client import T212Client, auth_header
from t212_mcp.config import Settings

BASE = "https://demo.trading212.com/api/v0"

SUMMARY = {
    "id": 1,
    "currency": "GBP",
    "totalValue": 1200.0,
    "cash": {"availableToTrade": 200.0, "inPies": 0, "reservedForOrders": 0},
    "investments": {"currentValue": 1000.0, "totalCost": 800.0, "unrealizedProfitLoss": 200.0, "realizedProfitLoss": 5.0},
}


def pos(ticker, name, qty, value, cost):
    return {
        "instrument": {"ticker": ticker, "name": name, "currency": "USD"},
        "quantity": qty,
        "averagePricePaid": 1.0,
        "currentPrice": 1.0,
        "walletImpact": {"currentValue": value, "totalCost": cost, "unrealizedProfitLoss": value - cost},
    }


POSITIONS = [pos("AAPL_US_EQ", "Apple", 2, 750.0, 500.0), pos("HIESl_EQ", "HSBC EM", 10, 250.0, 300.0)]


@pytest.fixture
def settings(database_url, monkeypatch):
    s = Settings(_env_file=None, api_key="key", api_secret="secret", env="demo", database_url=database_url)
    monkeypatch.setattr(server, "_client", T212Client(s))
    return s


def test_auth_header_basic_and_legacy():
    s = Settings(_env_file=None, api_key="k", api_secret="s")
    assert auth_header(s) == "Basic " + base64.b64encode(b"k:s").decode()
    assert auth_header(Settings(_env_file=None, api_key="k")) == "k"


def test_normalize_position_math():
    p = server.normalize_position(POSITIONS[0], invested_total=1000.0)
    assert p["pnl"] == 250.0 and p["pnl_pct"] == 50.0 and p["weight_pct"] == 75.0


def test_symbol():
    assert server.symbol("AAPL_US_EQ") == "AAPL"
    assert server.symbol("HIESl_EQ") == "HIES"


@respx.mock
async def test_positions_sorted_and_cached(settings):
    route = respx.get(f"{BASE}/equity/positions").respond(json=POSITIONS)
    async with Client(server.mcp) as c:
        r = await c.call_tool("get_positions", {"sort_by": "pnl"})
        await c.call_tool("get_positions", {})
    assert [p["ticker"] for p in r.data["positions"]] == ["AAPL_US_EQ", "HIESl_EQ"]
    assert route.call_count == 1  # second call served from cache


@respx.mock
async def test_get_position_by_plain_symbol(settings):
    respx.get(f"{BASE}/equity/positions").respond(json=POSITIONS)
    async with Client(server.mcp) as c:
        r = await c.call_tool("get_position", {"ticker": "hies"})
    assert r.data["name"] == "HSBC EM"


@respx.mock
async def test_retries_once_on_429(settings):
    respx.get(f"{BASE}/equity/account/summary").mock(
        side_effect=[
            httpx.Response(429, headers={"x-ratelimit-reset": str(time.time())}),
            httpx.Response(200, json=SUMMARY),
        ]
    )
    data = await server.client().get("/equity/account/summary")
    assert data["totalValue"] == 1200.0


@respx.mock
async def test_401_becomes_tool_error(settings):
    respx.get(f"{BASE}/equity/account/summary").respond(401)
    with pytest.raises(ToolError, match="rejected the credentials"):
        await server.client().get("/equity/account/summary")


@respx.mock
async def test_pagination_follows_next_page_path(settings):
    respx.get(f"{BASE}/equity/history/transactions", params={"cursor": "2"}).respond(
        json={"items": [{"amount": 2}], "nextPagePath": None}
    )
    respx.get(f"{BASE}/equity/history/transactions").respond(
        json={"items": [{"amount": 1}], "nextPagePath": "/api/v0/equity/history/transactions?limit=1&cursor=2"}
    )
    items = await server.client().get_paginated("/equity/history/transactions", limit=2)
    assert [i["amount"] for i in items] == [1, 2]


@respx.mock
async def test_portfolio_update_diffs_against_previous_snapshot(settings):
    respx.get(f"{BASE}/equity/account/summary").respond(json=SUMMARY)
    respx.get(f"{BASE}/equity/positions").respond(json=POSITIONS)
    prev = {
        "taken_at": "2026-01-01T00:00:00+00:00",
        "total_value": 1100.0,
        "invested_value": 900.0,
        "unrealized_pnl": 150.0,
        "cash_available": 200.0,
        "positions": {
            "AAPL_US_EQ": {"name": "Apple", "quantity": 1, "value": 600.0, "pnl": 100.0},
            "TSLA_US_EQ": {"name": "Tesla", "quantity": 1, "value": 100.0, "pnl": 0.0},
        },
    }
    snapshots.save(settings.database_url, "demo", prev)
    async with Client(server.mcp) as c:
        r = await c.call_tool("get_portfolio_update", {})
    d = r.data["since_last_update"]
    assert d["total_value_change"] == 100.0
    assert [p["ticker"] for p in d["positions_opened"]] == ["HIESl_EQ"]
    assert [p["ticker"] for p in d["positions_closed"]] == ["TSLA_US_EQ"]
    assert d["quantity_changes"] == [{"ticker": "AAPL_US_EQ", "name": "Apple", "from": 1, "to": 2}]
    assert d["biggest_pnl_gains"] == [{"ticker": "AAPL_US_EQ", "name": "Apple", "pnl_change": 150.0}]
    assert r.data["top_losers"][0]["ticker"] == "HIESl_EQ"
    assert snapshots.load_latest(settings.database_url, "demo")["total_value"] == 1200.0


async def test_bearer_auth_accepts_only_the_configured_token():
    verifier = server.bearer_auth("s3cret")
    assert await verifier.verify_token("s3cret") is not None
    assert await verifier.verify_token("wrong") is None
