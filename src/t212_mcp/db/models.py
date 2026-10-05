"""Relational data model (Postgres)."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, datetime: DateTime(timezone=True)}


# ---------------------------------------------------------------- portfolio snapshots


class PortfolioSnapshot(Base):
    """Account totals at one `get_portfolio_update` call; the next call diffs against the latest one."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_snapshots_env_taken_at", "env", "taken_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env: Mapped[str] = mapped_column(String(8))  # live | demo
    taken_at: Mapped[datetime]
    total_value: Mapped[float] = mapped_column(Float)
    invested_value: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    cash_available: Mapped[float] = mapped_column(Float)

    positions: Mapped[list["PortfolioPosition"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", lazy="selectin"
    )


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    snapshot_id: Mapped[int] = mapped_column(ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)

    snapshot: Mapped[PortfolioSnapshot] = relationship(back_populates="positions")


# ---------------------------------------------------------------- look-through


class Recipe(Base):
    """An extraction recipe. Superseded recipes are kept for history; only active ones are used."""

    __tablename__ = "recipes"
    __table_args__ = (Index("ix_recipes_active_scope", "superseded_at", "scope"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(String(8))  # issuer | isin
    issuer: Mapped[str] = mapped_column(String(120))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    spec: Mapped[dict]  # the full recipes.Recipe model
    discovered_at: Mapped[datetime]
    discovered_by: Mapped[str] = mapped_column(String(120))
    superseded_at: Mapped[datetime | None]
    superseded_by: Mapped[str | None] = mapped_column(ForeignKey("recipes.id", ondelete="SET NULL"))


class Fund(Base):
    """Current look-through status of one held ETF."""

    __tablename__ = "funds"

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # ok | stale | unresolved
    checked_at: Mapped[datetime]
    recipe_id: Mapped[str | None] = mapped_column(ForeignKey("recipes.id", ondelete="SET NULL"))
    last_error: Mapped[str | None] = mapped_column(Text)
    validation_report: Mapped[dict | None]


class HoldingsSnapshot(Base):
    """One validated download of a fund's holdings. The latest one is the fund's last good data."""

    __tablename__ = "holdings_snapshots"
    __table_args__ = (Index("ix_holdings_snapshots_fund_fetched", "fund_isin", "fetched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_isin: Mapped[str] = mapped_column(ForeignKey("funds.isin", ondelete="CASCADE"))
    recipe_id: Mapped[str | None] = mapped_column(ForeignKey("recipes.id", ondelete="SET NULL"))
    as_of: Mapped[str | None] = mapped_column(String(32))  # as published by the issuer
    coverage: Mapped[str] = mapped_column(String(10))  # full | partial
    source_url: Mapped[str] = mapped_column(Text)
    source_excerpt: Mapped[str] = mapped_column(Text, default="")
    countries: Mapped[dict] = mapped_column(default=dict)  # official country split, if the issuer publishes one
    sectors: Mapped[dict] = mapped_column(default=dict)
    fetched_at: Mapped[datetime]

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="Holding.position", lazy="selectin"
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("holdings_snapshots.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # order in the source
    name: Mapped[str] = mapped_column(Text)
    weight_pct: Mapped[float] = mapped_column(Float)
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    country: Mapped[str | None] = mapped_column(String(80))
    sector: Mapped[str | None] = mapped_column(String(120))

    snapshot: Mapped[HoldingsSnapshot] = relationship(back_populates="holdings")


class ExposureSnapshot(Base):
    """The combined look-through exposure for one day. Headline numbers are columns; the breakdowns are JSON."""

    __tablename__ = "exposure_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, unique=True)
    created_at: Mapped[datetime]
    invested_value: Mapped[float] = mapped_column(Float)
    securities_count: Mapped[int] = mapped_column(Integer)
    top10_pct: Mapped[float] = mapped_column(Float)
    cash_inside_funds: Mapped[float] = mapped_column(Float)
    funds_with_data_pct: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict]  # the full exposure dict


class AgentRun(Base):
    """Events from the discovery agent; also used to enforce the daily run limit."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(index=True)
    event: Mapped[str] = mapped_column(String(20))  # agent_start | recipe_saved | agent_failed
    isin: Mapped[str | None] = mapped_column(String(12))
    model: Mapped[str | None] = mapped_column(String(120))
    recipe_id: Mapped[str | None] = mapped_column(String(80))
    tokens: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
