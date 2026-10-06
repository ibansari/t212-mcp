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


PERSONAL_TABLES = ("portfolio_snapshots", "exposure_snapshots", "digest_baselines")
UNIQUE = {"exposure_snapshots": ("uq_exposure_snapshots_user_day", "user_id, as_of", "exposure_snapshots_as_of_key"),
          "digest_baselines": ("uq_digest_baselines_user_env_day", "user_id, env, day", "digest_baselines_env_day_key")}


def upgrade_multiuser(url: str, owner_id: str = "owner") -> list[str]:
    """Bring a single-user database to the multi-user schema (idempotent). Existing personal rows become the
    owner's; the obsolete sign-in session table (oauth_kv) is dropped. New databases already have this schema."""
    from sqlalchemy import text

    done = []
    with engine(url).begin() as conn:
        def run(sql: str, **params) -> None:
            conn.execute(text(sql), params)
            done.append(sql.split("\n")[0][:90])

        for table in PERSONAL_TABLES:
            run(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id VARCHAR(64)")
            run(f"UPDATE {table} SET user_id = :owner WHERE user_id IS NULL", owner=owner_id)
            run(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL")
        run("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS user_id VARCHAR(64)")
        run("CREATE INDEX IF NOT EXISTS ix_agent_runs_user_id ON agent_runs (user_id)")
        run("DROP INDEX IF EXISTS ix_portfolio_snapshots_env_taken_at")
        run("CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_user_env_taken_at ON portfolio_snapshots (user_id, env, taken_at)")
        for table, (name, columns, old) in UNIQUE.items():
            run(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old}")
            exists = conn.scalar(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name})
            if not exists:
                run(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({columns})")
        run("DROP TABLE IF EXISTS oauth_kv")
    return done
