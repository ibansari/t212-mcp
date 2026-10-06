"""Look-through state in the database: last good holdings per fund, fund status, daily exposure snapshots,
agent run log."""

from datetime import date, datetime, timezone

from sqlalchemy import func, select

from .. import db
from ..db import models as m
from .extractors import FundHoldings, Holding


class Store:
    def __init__(self, database_url: str):
        self.sessions = db.sessions(database_url)

    # ---- per-fund holdings + status
    @staticmethod
    def _fund_dict(f: m.Fund) -> dict:
        return {"isin": f.isin, "status": f.status, "checked_at": db.iso(f.checked_at), "recipe_id": f.recipe_id,
                "report": f.validation_report, "error": f.last_error}

    def load_fund(self, isin: str) -> dict | None:
        with self.sessions() as s:
            f = s.get(m.Fund, isin)
            return self._fund_dict(f) if f else None

    def last_good(self, isin: str) -> FundHoldings | None:
        with self.sessions() as s:
            snap = s.scalars(
                select(m.HoldingsSnapshot).where(m.HoldingsSnapshot.fund_isin == isin)
                .order_by(m.HoldingsSnapshot.fetched_at.desc(), m.HoldingsSnapshot.id.desc()).limit(1)
            ).first()
            if snap is None:
                return None
            return FundHoldings(
                isin=isin, as_of=snap.as_of, coverage=snap.coverage, countries=snap.countries, sectors=snap.sectors,
                source_url=snap.source_url, source_excerpt=snap.source_excerpt, recipe_id=snap.recipe_id,
                fetched_at=db.iso(snap.fetched_at) or "",
                holdings=[Holding(name=h.name, weight_pct=h.weight_pct, isin=h.isin, country=h.country, sector=h.sector)
                          for h in snap.holdings],
            )

    def save_fund(self, isin: str, *, status: str, holdings: FundHoldings | None = None, **meta) -> None:
        """Update a fund's status. Keyword fields recipe_id, report and error are written when given."""
        now = db.utcnow()
        with self.sessions.begin() as s:
            f = s.get(m.Fund, isin) or m.Fund(isin=isin)
            f.status, f.checked_at = status, now
            if "recipe_id" in meta:
                f.recipe_id = meta["recipe_id"]
            if "report" in meta:
                f.validation_report = meta["report"]
            if "error" in meta:
                f.last_error = meta["error"]
            s.add(f)
            if holdings is None:
                return
            s.flush()
            if holdings.as_of:  # a re-download of the same publication replaces the earlier copy
                for old in s.scalars(select(m.HoldingsSnapshot).where(
                        m.HoldingsSnapshot.fund_isin == isin, m.HoldingsSnapshot.as_of == holdings.as_of)):
                    s.delete(old)
                s.flush()
            s.add(m.HoldingsSnapshot(
                fund_isin=isin, recipe_id=holdings.recipe_id or meta.get("recipe_id"), as_of=holdings.as_of,
                coverage=holdings.coverage, source_url=holdings.source_url, source_excerpt=holdings.source_excerpt,
                countries=holdings.countries, sectors=holdings.sectors,
                fetched_at=db.aware(datetime.fromisoformat(holdings.fetched_at)).astimezone(timezone.utc) if holdings.fetched_at else now,
                holdings=[m.Holding(position=i, name=h.name, weight_pct=h.weight_pct, isin=h.isin, country=h.country,
                                    sector=h.sector) for i, h in enumerate(holdings.holdings)],
            ))

    def all_funds(self) -> list[dict]:
        with self.sessions() as s:
            return [self._fund_dict(f) for f in s.scalars(select(m.Fund).order_by(m.Fund.isin))]

    # ---- exposure snapshots
    def save_exposure(self, exposure: dict, day: date | None = None) -> int:
        day = day or date.today()
        with self.sessions.begin() as s:
            row = s.scalars(select(m.ExposureSnapshot).where(m.ExposureSnapshot.as_of == day)).first()
            row = row or m.ExposureSnapshot(as_of=day)
            row.created_at = db.utcnow()
            row.invested_value = exposure["invested_value"]
            row.securities_count = exposure["securities_count"]
            row.top10_pct = exposure["top10_pct"]
            row.cash_inside_funds = exposure["cash_inside_funds"]
            row.funds_with_data_pct = exposure["coverage"]["funds_with_data_pct"]
            row.payload = exposure
            s.add(row)
            s.flush()
            return row.id

    def exposure_history(self) -> list[tuple[str, int]]:
        """(ISO day, snapshot id), oldest first."""
        with self.sessions() as s:
            rows = s.execute(select(m.ExposureSnapshot.as_of, m.ExposureSnapshot.id).order_by(m.ExposureSnapshot.as_of))
            return [(d.isoformat(), i) for d, i in rows]

    def load_exposure(self, snapshot_id: int) -> dict:
        with self.sessions() as s:
            return s.get_one(m.ExposureSnapshot, snapshot_id).payload

    # ---- agent run log
    def log_run(self, entry: dict) -> None:
        fields = {k: entry.get(k) for k in ("event", "isin", "model", "recipe_id", "tokens", "attempts", "error")}
        with self.sessions.begin() as s:
            s.add(m.AgentRun(at=db.utcnow(), **fields))

    def runs(self, limit: int | None = None) -> list[dict]:
        """Agent events, oldest first (the most recent `limit` if given)."""
        with self.sessions() as s:
            q = select(m.AgentRun).order_by(m.AgentRun.at.desc(), m.AgentRun.id.desc())
            rows = list(s.scalars(q.limit(limit) if limit else q))
        out = []
        for r in reversed(rows):
            d = {"at": db.iso(r.at), "event": r.event, "isin": r.isin, "model": r.model, "recipe_id": r.recipe_id,
                 "tokens": r.tokens, "attempts": r.attempts, "error": r.error}
            out.append({k: v for k, v in d.items() if v is not None})
        return out

    # ---- security entities
    ENTITY_FIELDS = ("entity_key", "name", "figi_name", "ticker", "exch_code", "security_type", "method", "reason")

    def entities(self, isins: list[str] | None = None) -> dict[str, dict]:
        with self.sessions() as s:
            q = select(m.SecurityEntity)
            if isins is not None:
                q = q.where(m.SecurityEntity.isin.in_(isins))
            return {e.isin: {f: getattr(e, f) for f in self.ENTITY_FIELDS} for e in s.scalars(q)}

    def save_entities(self, rows: dict[str, dict]) -> None:
        now = db.utcnow()
        with self.sessions.begin() as s:
            for isin, row in rows.items():
                e = s.get(m.SecurityEntity, isin) or m.SecurityEntity(isin=isin)
                for f in self.ENTITY_FIELDS:
                    setattr(e, f, row.get(f))
                e.decided_at = now
                s.add(e)

    def decided_clusters(self) -> set[frozenset]:
        with self.sessions() as s:
            return {frozenset(c.split(" | ")) for c in s.scalars(select(m.EntityDecision.cluster))}

    def mark_decided(self, clusters: list[frozenset]) -> None:
        now = db.utcnow()
        with self.sessions.begin() as s:
            for c in clusters:
                key = " | ".join(sorted(c))[:1000]
                if s.get(m.EntityDecision, key) is None:
                    s.add(m.EntityDecision(cluster=key, decided_at=now))

    # ---- agent traces
    def save_trace(self, *, isin: str, ticker: str, step: str, model: str | None, tree: dict, tokens: int,
                   keep: int = 30) -> None:
        with self.sessions.begin() as s:
            s.add(m.AgentTrace(at=db.utcnow(), isin=isin, ticker=ticker, step=step, model=model, tokens=tokens,
                               duration_ms=tree.get("ms"), error=tree.get("error"), tree=tree))
            s.flush()
            old = s.scalars(select(m.AgentTrace.id).where(m.AgentTrace.isin == isin)
                            .order_by(m.AgentTrace.at.desc(), m.AgentTrace.id.desc()).offset(keep)).all()
            for trace_id in old:
                s.delete(s.get(m.AgentTrace, trace_id))

    def traces(self, fund: str, limit: int = 5) -> list[dict]:
        """Newest first; `fund` is an ISIN or a ticker."""
        key = fund.strip().upper()
        with self.sessions() as s:
            rows = s.scalars(select(m.AgentTrace).where((m.AgentTrace.isin == key) | (func.upper(m.AgentTrace.ticker) == key))
                             .order_by(m.AgentTrace.at.desc(), m.AgentTrace.id.desc()).limit(limit))
            return [{"at": db.iso(r.at), "isin": r.isin, "ticker": r.ticker, "step": r.step, "model": r.model,
                     "tokens": r.tokens, "duration_ms": r.duration_ms, "error": r.error, "tree": r.tree} for r in rows]

    def agent_runs_today(self) -> int:
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        with self.sessions() as s:
            return s.scalar(select(func.count()).select_from(m.AgentRun).where(
                m.AgentRun.event == "agent_start", m.AgentRun.at >= midnight)) or 0
