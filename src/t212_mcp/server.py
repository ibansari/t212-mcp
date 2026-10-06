"""FastMCP server exposing a read-only view of a Trading 212 portfolio."""

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import accounts, snapshots
from .client import T212Client
from .config import Settings

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP(
    "trading212",
    instructions=(
        "Read-only access to the user's Trading 212 invest account. Use get_portfolio_update for a "
        "'how is my portfolio doing' style question; it also reports changes since the previous update. "
        "Monetary values are in the account currency unless a field says otherwise."
    ),
)

_client: T212Client | None = None  # pinned client (tests); otherwise each caller gets their own


async def client() -> T212Client:
    """The calling user's Trading 212 client (the owner's in stdio/CLI)."""
    return _client if _client is not None else await accounts.require_client()


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def normalize_summary(raw: dict) -> dict:
    cash = raw.get("cash") or {}
    inv = raw.get("investments") or {}
    unrealized = inv.get("unrealizedProfitLoss", 0.0)
    cost = inv.get("totalCost", 0.0)
    return {
        "account_id": raw.get("id"),
        "currency": raw.get("currency"),
        "total_value": raw.get("totalValue", 0.0),
        "cash": {
            "available_to_trade": cash.get("availableToTrade", 0.0),
            "in_pies": cash.get("inPies", 0.0),
            "reserved_for_orders": cash.get("reservedForOrders", 0.0),
        },
        "investments": {
            "current_value": inv.get("currentValue", 0.0),
            "total_cost": cost,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": _r(unrealized / cost * 100) if cost else None,
            "realized_pnl": inv.get("realizedProfitLoss", 0.0),
        },
    }


def normalize_position(raw: dict, invested_total: float) -> dict:
    inst = raw.get("instrument") or {}
    wallet = raw.get("walletImpact") or {}
    value = wallet.get("currentValue", 0.0)
    cost = wallet.get("totalCost", 0.0)
    pnl = wallet.get("unrealizedProfitLoss", value - cost)
    return {
        "ticker": inst.get("ticker"),
        "name": inst.get("name") or inst.get("ticker"),
        "isin": inst.get("isin"),
        "instrument_currency": inst.get("currency"),
        "quantity": raw.get("quantity", 0.0),
        "average_price": raw.get("averagePricePaid"),
        "current_price": raw.get("currentPrice"),
        "value": _r(value),
        "cost": _r(cost),
        "pnl": _r(pnl),
        "pnl_pct": _r(pnl / cost * 100) if cost else None,
        "fx_impact": _r(wallet.get("fxImpact")),
        "weight_pct": _r(value / invested_total * 100) if invested_total else None,
        "opened_at": raw.get("createdAt"),
    }


async def _fetch_summary(c: T212Client | None = None) -> dict:
    return normalize_summary(await (c or await client()).get("/equity/account/summary", ttl=5))


async def _fetch_positions(c: T212Client | None = None) -> list[dict]:
    raw = await (c or await client()).get("/equity/positions", ttl=2)
    invested = sum((p.get("walletImpact") or {}).get("currentValue", 0.0) for p in raw)
    return [normalize_position(p, invested) for p in raw]


def symbol(t212_ticker: str) -> str:
    """Plain symbol from a Trading 212 ticker: AAPL_US_EQ -> AAPL, HIESl_EQ -> HIES (LSE 'l' suffix)."""
    base = t212_ticker.split("_")[0]
    if base.endswith("l") and base[:-1].isupper():
        base = base[:-1]
    return base.upper()


SORT_KEYS = {"value": "value", "pnl": "pnl", "pnl_pct": "pnl_pct", "weight": "weight_pct"}


@mcp.tool(annotations=READ_ONLY)
async def get_account_summary() -> dict:
    """Account overview: total value, cash (available / in pies / reserved), invested value, cost basis and P&L."""
    return await _fetch_summary()


@mcp.tool(annotations=READ_ONLY)
async def get_positions(
    sort_by: Literal["value", "pnl", "pnl_pct", "weight"] = "value",
    limit: int | None = None,
) -> dict:
    """All open positions with quantity, average/current price, value, P&L, P&L % and portfolio weight.

    Sorted descending by `sort_by`; `limit` returns only the top N.
    """
    positions = await _fetch_positions()
    key = SORT_KEYS[sort_by]
    positions.sort(key=lambda p: p[key] if p[key] is not None else float("-inf"), reverse=True)
    return {"count": len(positions), "positions": positions[:limit] if limit else positions}


@mcp.tool(annotations=READ_ONLY)
async def get_position(ticker: str) -> dict:
    """One position in detail. `ticker` is the Trading 212 ticker (e.g. AAPL_US_EQ) or a plain symbol like AAPL."""
    positions = await _fetch_positions()
    wanted = ticker.upper()
    for p in positions:
        if wanted in ((p["ticker"] or "").upper(), symbol(p["ticker"] or "")):
            return p
    raise ToolError(f"No open position matching '{ticker}'. Held: {', '.join(p['ticker'] for p in positions)}")


@mcp.tool(annotations={**READ_ONLY, "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def get_portfolio_update() -> dict:
    """The main 'how is my portfolio doing?' tool.

    Returns totals, top gainers/losers, concentration, and what changed since the previous call
    (value / P&L change, positions opened or closed, quantity changes). Saves a snapshot to the
    database each call so the next update can diff against it; it never trades.
    """
    summary = await _fetch_summary()
    positions = await _fetch_positions()
    by_pnl = sorted(positions, key=lambda p: p["pnl_pct"] if p["pnl_pct"] is not None else 0.0)
    by_weight = sorted(positions, key=lambda p: p["value"] or 0.0, reverse=True)

    def brief(p: dict) -> dict:
        return {k: p[k] for k in ("ticker", "name", "value", "pnl", "pnl_pct", "weight_pct")}

    settings, user, env = accounts.get_settings(), await accounts.current_user(), (await client()).creds.env
    current = snapshots.make_snapshot(summary, positions)
    previous = snapshots.load_latest(settings.database_url, env, user_id=user.id)
    snapshots.save(settings.database_url, env, current, user_id=user.id)

    return {
        "as_of": current["taken_at"],
        "summary": summary,
        "position_count": len(positions),
        "top_gainers": [brief(p) for p in reversed(by_pnl[-3:]) if (p["pnl_pct"] or 0) > 0],
        "top_losers": [brief(p) for p in by_pnl[:3] if (p["pnl_pct"] or 0) < 0],
        "concentration": {
            "largest_holdings": [brief(p) for p in by_weight[:5]],
            "top5_weight_pct": _r(sum(p["weight_pct"] or 0 for p in by_weight[:5])),
        },
        "since_last_update": snapshots.diff(previous, current) if previous else None,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_dividends(limit: int = 20, ticker: str | None = None) -> dict:
    """Recent dividends paid (newest first) and their total, optionally for one ticker."""
    items = await (await client()).get_paginated("/equity/history/dividends", limit, {"ticker": ticker})
    dividends = [
        {
            "ticker": d.get("ticker"),
            "name": (d.get("instrument") or {}).get("name"),
            "paid_on": d.get("paidOn"),
            "amount": d.get("amount"),
            "currency": d.get("currency"),
            "quantity": d.get("quantity"),
            "gross_per_share": d.get("grossAmountPerShare"),
            "type": d.get("type"),
        }
        for d in items
    ]
    return {"count": len(dividends), "total": _r(sum(d["amount"] or 0 for d in dividends)), "dividends": dividends}


@mcp.tool(annotations=READ_ONLY)
async def get_order_history(limit: int = 20, ticker: str | None = None) -> dict:
    """Recent historical orders (newest first) with fill details, optionally for one ticker."""
    items = await (await client()).get_paginated("/equity/history/orders", limit, {"ticker": ticker})
    orders = []
    for item in items:
        o = item.get("order") or {}
        f = item.get("fill") or {}
        impact = f.get("walletImpact") or {}
        orders.append(
            {
                "id": o.get("id"),
                "ticker": o.get("ticker"),
                "name": (o.get("instrument") or {}).get("name"),
                "side": o.get("side"),
                "type": o.get("type"),
                "status": o.get("status"),
                "created_at": o.get("createdAt"),
                "filled_at": f.get("filledAt"),
                "quantity": f.get("quantity") or o.get("filledQuantity") or o.get("quantity"),
                "fill_price": f.get("price"),
                "net_value": impact.get("netValue"),
                "realized_pnl": impact.get("realisedProfitLoss"),
                "currency": impact.get("currency") or o.get("currency"),
            }
        )
    return {"count": len(orders), "orders": orders}


@mcp.tool(annotations=READ_ONLY)
async def get_transactions(limit: int = 20) -> dict:
    """Recent cash movements: deposits, withdrawals, fees, transfers and interest."""
    items = await (await client()).get_paginated("/equity/history/transactions", limit)
    return {"count": len(items), "transactions": items}


def _lookthrough_store():
    from .lookthrough.store import Store

    return Store(accounts.get_settings().database_url)


async def _caller_funds() -> tuple[accounts.User, list[dict], set[str]]:
    """The caller, their positions, and the ISINs of theirs that have look-through data (i.e. their funds)."""
    user = await accounts.current_user()
    positions = await _fetch_positions()
    store = _lookthrough_store()
    funds = {p["isin"] for p in positions if p.get("isin") and store.load_fund(p["isin"])}
    return user, positions, funds


@mcp.tool(annotations=READ_ONLY)
async def get_etf_exposure(top_n: int = 25, group_by: Literal["security", "country", "sector"] = "security") -> dict:
    """Look-through exposure: what you really own once each ETF is broken into its holdings, combined with your
    direct stocks (e.g. Nvidia held directly plus via several ETFs). Group by underlying security, country or sector.

    Computed now from your current positions and each fund's latest stored holdings; check `coverage` for funds
    without data or with old data (refresh with refresh_etf_holdings).
    """
    from datetime import date

    from .digest import look_through

    exp = look_through(_lookthrough_store(), await _fetch_positions())
    if exp is None:
        raise ToolError("No look-through data for your funds yet. Run refresh_etf_holdings, then ask again.")
    exp["as_of"] = date.today().isoformat()
    out = {k: exp[k] for k in ("as_of", "invested_value", "securities_count", "top10_pct", "cash_inside_funds", "coverage")}
    if group_by == "security":
        out["top_securities"] = exp["all_securities"][:top_n]
    elif group_by == "country":
        out["countries"] = exp["countries"][:top_n]
    else:
        out["sectors"] = exp["sectors"][:top_n]
        unknown = sum(s["pct"] for s in exp["sectors"] if s["name"].startswith("Unknown"))
        if unknown >= 90:
            out["note"] = "Sector data is unavailable: the issuers' holdings files used here don't include sectors."
    return out


@mcp.tool(annotations=READ_ONLY)
async def get_exposure_changes(days: int = 7) -> dict:
    """How look-through exposure shifted between the latest refresh and the one `days` ago (or the oldest available)."""
    from datetime import date, timedelta

    from .lookthrough.exposure import exposure_changes

    store, uid = _lookthrough_store(), (await accounts.current_user()).id
    history = store.exposure_history(user_id=uid)
    if len(history) < 2:
        raise ToolError("Need at least two daily look-through snapshots to compare (one is saved per refresh day).")
    target = (date.today() - timedelta(days=days)).isoformat()
    older = [h for h in history[:-1] if h[0] <= target] or history[:1]
    old_day, old_path = older[-1]
    new_day, new_path = history[-1]
    return {"from": old_day, "to": new_day, **exposure_changes(store.load_exposure(old_path, user_id=uid),
                                                               store.load_exposure(new_path, user_id=uid))}


@mcp.tool(annotations=READ_ONLY)
async def get_holdings_status() -> dict:
    """Health of the look-through pipeline for your funds: per fund, its status (ok/stale/unresolved), recipe,
    holdings date and last error; plus your latest refresh, recent agent runs and the configured LLM."""
    from .lookthrough.recipes import Registry

    settings = accounts.get_settings()
    store = _lookthrough_store()
    user, positions, _ = await _caller_funds()
    held = {p["isin"] for p in positions if p.get("isin")}
    recipes = {r.id: r for r in Registry(settings.database_url).all()}
    funds = []
    for rec in store.all_funds():
        if rec["isin"] not in held and not user.is_admin:  # other users' funds stay private
            continue
        r = recipes.get(rec.get("recipe_id") or "")
        stats = (rec.get("report") or {}).get("stats", {})
        funds.append(
            {
                "isin": rec["isin"],
                "status": rec.get("status"),
                "checked_at": rec.get("checked_at"),
                "holdings_as_of": stats.get("as_of"),
                "holdings_count": stats.get("holdings"),
                "recipe": {"id": r.id, "kind": r.kind, "scope": r.scope, "issuer": r.issuer, "discovered_by": r.discovered_by} if r else None,
                "error": rec.get("error"),
            }
        )
    return {
        "llm_model": settings.llm_model,
        "refresh": dict(_refresh.get(user.id, {"state": "idle"})),
        "funds": funds,
        "recent_agent_runs": store.runs(limit=10, user_id=None if user.is_admin else user.id),
    }


@mcp.tool(annotations=READ_ONLY)
async def get_agent_trace(fund: str, limit: int = 3) -> dict:
    """Recent discovery-agent traces for one fund (ticker or ISIN), newest first: each model and tool step with its
    inputs, outputs, tokens, timings and errors, as a tree. Use it to see why the agent failed to find or fix a
    fund's holdings source. Only for funds you hold (admins: any fund, and `ENTITIES` for company matching). Long
    texts are truncated."""
    user, positions, funds = await _caller_funds()
    held = {k.upper() for p in positions if p.get("isin") in funds for k in (p["isin"], symbol(p["ticker"]))}
    if not user.is_admin and fund.strip().upper() not in held:
        raise ToolError(f"'{fund}' isn't one of your funds with look-through data.")
    traces = _lookthrough_store().traces(fund, limit=min(limit, 10))
    if not traces:
        raise ToolError(f"No agent traces for '{fund}'. Traces are recorded when the agent researches a fund.")
    return {"fund": fund, "traces": traces}


# The current/last background refresh per user. A refresh can take minutes, longer than proxies in front of the
# HTTP server allow for one request, so the tool starts it and get_holdings_status reports on it.
_refresh: dict[int, dict] = {}
_refresh_tasks: dict[int, asyncio.Task] = {}
REFRESH_COOLDOWN_S = 600  # per non-admin user


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run_refresh(settings: Settings, user: accounts.User, c: T212Client, allow_agent: bool) -> None:
    from .lookthrough.graph import RefreshBusy, refresh

    status = _refresh[user.id]
    try:
        exp = await refresh(settings, allow_agent=allow_agent, client=c, user_id=user.id)
        status.update(state="done", finished_at=_now(), as_of=exp["as_of"], results=exp["refresh_results"],
                      funds_with_data_pct=exp["coverage"]["funds_with_data_pct"])
    except RefreshBusy:
        status.update(state="failed", finished_at=_now(), error="another refresh is running; try again in a minute")
    except Exception as e:
        logging.getLogger("t212_mcp").exception("look-through refresh failed")
        status.update(state="failed", finished_at=_now(), error=f"{type(e).__name__}: {e}")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def refresh_etf_holdings(allow_agent: bool = False) -> dict:
    """Start a look-through refresh in the background and return immediately; it does not wait for the result.
    Funds with a working recipe are re-downloaded (fast, no LLM). With allow_agent=True, funds without a working
    recipe are researched by the discovery agent, which can take several minutes and uses the configured LLM.
    Call get_holdings_status to follow progress (its `refresh` field) and see per-fund results. The agent is
    available to admins only; for others, funds without a saved source are left for an admin to research."""
    user = await accounts.current_user()
    c = await client()
    task, status = _refresh_tasks.get(user.id), _refresh.get(user.id, {})
    if task is not None and not task.done():
        return {"started": False, "reason": "a refresh is already running", **status}
    if not user.is_admin and status.get("started_at"):
        wait = REFRESH_COOLDOWN_S - (datetime.now(timezone.utc) - datetime.fromisoformat(status["started_at"])).total_seconds()
        if wait > 0:
            return {"started": False, "reason": f"refreshed recently; try again in {int(wait // 60) + 1} min", **status}
    allow_agent = allow_agent and user.is_admin
    _refresh[user.id] = {"state": "running", "allow_agent": allow_agent, "started_at": _now()}
    _refresh_tasks[user.id] = asyncio.create_task(_run_refresh(accounts.get_settings(), user, c, allow_agent))
    return {"started": True, **_refresh[user.id]}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    """Liveness check for load balancers; needs no token."""
    return PlainTextResponse("ok")


from . import account_page  # noqa: E402  (the /account routes)

account_page.register(mcp)


@mcp.resource("portfolio://summary", mime_type="application/json")
async def portfolio_summary() -> dict:
    """Current account summary."""
    return await _fetch_summary()


@mcp.prompt
def daily_briefing() -> str:
    """Ask for a short daily briefing on the portfolio."""
    return (
        "Call get_portfolio_update and write me a concise portfolio briefing: total value and P&L, "
        "what changed since the last update, the biggest winners and losers, and any concentration risk "
        "worth flagging. Keep it under 200 words and don't give buy/sell advice."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading 212 MCP server")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "refresh-holdings", "resolve-entities", "send-digest", "upgrade-multiuser",
                                 "install-schedule", "uninstall-schedule"])
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--allow-agent", action="store_true", help="let the LLM agent research funds without a working recipe")
    parser.add_argument("--at", default="07:30", help="install-schedule: daily time HH:MM")
    parser.add_argument("--preview", type=Path, metavar="FILE.html", help="send-digest: write the email locally instead")
    parser.add_argument("--only-at-local-hour", type=int, metavar="H",
                        help="send-digest: do nothing unless it is hour H in --tz (lets a UTC cron follow UK time)")
    parser.add_argument("--tz", default="Europe/London", help="send-digest: time zone for --only-at-local-hour")
    args = parser.parse_args()

    if args.command == "refresh-holdings":
        import json

        from .lookthrough.graph import refresh

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
        for noisy in ("httpx", "httpx2", "httpcore", "openai", "primp", "ddgs"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        exp = asyncio.run(refresh(accounts.get_settings(), allow_agent=args.allow_agent))
        print(json.dumps({"as_of": exp["as_of"], "results": exp["refresh_results"],
                          "funds_with_data_pct": exp["coverage"]["funds_with_data_pct"]}, indent=2))
    elif args.command == "resolve-entities":
        from . import digest
        from .lookthrough import entities
        from .lookthrough.store import Store

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
        for noisy in ("httpx", "httpx2", "httpcore", "openai", "primp", "ddgs"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        async def resolve_now() -> dict:
            settings = accounts.get_settings()
            store = Store(settings.database_url)
            exposure = digest.look_through(store, await _fetch_positions())
            if not exposure:
                return {"error": "no look-through data; run refresh-holdings first"}
            return await entities.resolve(store, settings, exposure["all_securities"], allow_agent=True)

        print(asyncio.run(resolve_now()))
    elif args.command == "upgrade-multiuser":
        from . import db

        for step in db.upgrade_multiuser(accounts.get_settings().database_url, accounts.OWNER_ID):
            print(step)
        print("Database upgraded to the multi-user schema")
    elif args.command == "send-digest":
        from . import digest

        if args.only_at_local_hour is not None and not digest.is_local_hour(args.only_at_local_hour, args.tz):
            print(f"Not {args.only_at_local_hour}:00 in {args.tz}; skipping")
            return
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
        print(asyncio.run(digest.run(accounts.get_settings(), preview=args.preview)))
    elif args.command == "install-schedule":
        from .lookthrough.schedule import install

        hour, minute = (int(x) for x in args.at.split(":"))
        print(install(accounts.get_settings(), hour, minute))
    elif args.command == "uninstall-schedule":
        from .lookthrough.schedule import uninstall

        print(uninstall())
    elif args.http:
        from .auth import ConfigError, http_auth

        try:
            mcp.auth = http_auth(accounts.get_settings())
        except ConfigError as e:
            parser.error(str(e))
        if mcp.auth is None and args.host not in ("127.0.0.1", "localhost", "::1"):
            parser.error("set up AuthKit sign-in (T212_AUTHKIT_DOMAIN, ...) or T212_MCP_AUTH_TOKEN before serving "
                         "HTTP on a non-local address")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()
