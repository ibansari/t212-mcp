"""Database engine and session helpers. Tables are created on first use."""

from datetime import datetime, timezone
from functools import cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

@cache
def engine(url: str) -> Engine:
    eng = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(eng)
    return eng


@cache
def sessions(url: str) -> sessionmaker[Session]:
    return sessionmaker(engine(url), expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return aware(dt).isoformat(timespec="seconds") if dt else None
