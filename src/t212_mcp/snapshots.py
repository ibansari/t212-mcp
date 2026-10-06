"""Persist portfolio snapshots so updates can report what changed since the last check."""

from datetime import datetime, timezone

from sqlalchemy import select

from . import db
from .db import models as m

QTY_EPSILON = 1e-9


def make_snapshot(summary: dict, positions: list[dict]) -> dict:
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_value": summary["total_value"],
        "invested_value": summary["investments"]["current_value"],
        "unrealized_pnl": summary["investments"]["unrealized_pnl"],
        "cash_available": summary["cash"]["available_to_trade"],
        "positions": {
            p["ticker"]: {"name": p["name"], "quantity": p["quantity"], "value": p["value"], "pnl": p["pnl"]}
            for p in positions
        },
    }


def load_latest(database_url: str, env: str, *, user_id: str, before: datetime | None = None) -> dict | None:
    with db.sessions(database_url)() as s:
        q = select(m.PortfolioSnapshot).where(m.PortfolioSnapshot.user_id == user_id, m.PortfolioSnapshot.env == env)
        if before is not None:
            q = q.where(m.PortfolioSnapshot.taken_at < before)
        snap = s.scalars(q.order_by(m.PortfolioSnapshot.taken_at.desc(), m.PortfolioSnapshot.id.desc()).limit(1)).first()
        if snap is None:
            return None
        return {
            "taken_at": db.iso(snap.taken_at),
            "total_value": snap.total_value,
            "invested_value": snap.invested_value,
            "unrealized_pnl": snap.unrealized_pnl,
            "cash_available": snap.cash_available,
            "positions": {p.ticker: {"name": p.name, "quantity": p.quantity, "value": p.value, "pnl": p.pnl}
                          for p in snap.positions},
        }


def save(database_url: str, env: str, snapshot: dict, *, user_id: str) -> None:
    taken_at = db.aware(datetime.fromisoformat(snapshot["taken_at"])).astimezone(timezone.utc)
    with db.sessions(database_url).begin() as s:
        s.add(m.PortfolioSnapshot(
            user_id=user_id, env=env, taken_at=taken_at, total_value=snapshot["total_value"],
            invested_value=snapshot["invested_value"], unrealized_pnl=snapshot["unrealized_pnl"],
            cash_available=snapshot["cash_available"],
            positions=[m.PortfolioPosition(ticker=t, **p) for t, p in snapshot["positions"].items()],
        ))


def diff(prev: dict, cur: dict) -> dict:
    prev_pos, cur_pos = prev["positions"], cur["positions"]
    opened = [{"ticker": t, "name": p["name"], "quantity": p["quantity"]} for t, p in cur_pos.items() if t not in prev_pos]
    closed = [{"ticker": t, "name": p["name"], "quantity": p["quantity"]} for t, p in prev_pos.items() if t not in cur_pos]
    quantity_changes = []
    movers = []
    for t in cur_pos.keys() & prev_pos.keys():
        before, after = prev_pos[t], cur_pos[t]
        if abs(after["quantity"] - before["quantity"]) > QTY_EPSILON:
            quantity_changes.append(
                {"ticker": t, "name": after["name"], "from": before["quantity"], "to": after["quantity"]}
            )
        movers.append({"ticker": t, "name": after["name"], "pnl_change": round(after["pnl"] - before["pnl"], 2)})
    movers.sort(key=lambda m: m["pnl_change"])
    return {
        "since": prev["taken_at"],
        "total_value_change": round(cur["total_value"] - prev["total_value"], 2),
        "unrealized_pnl_change": round(cur["unrealized_pnl"] - prev["unrealized_pnl"], 2),
        "cash_change": round(cur["cash_available"] - prev["cash_available"], 2),
        "positions_opened": opened,
        "positions_closed": closed,
        "quantity_changes": quantity_changes,
        "biggest_pnl_gains": [m for m in reversed(movers[-3:]) if m["pnl_change"] > 0],
        "biggest_pnl_drops": [m for m in movers[:3] if m["pnl_change"] < 0],
    }
