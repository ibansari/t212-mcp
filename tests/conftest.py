"""Tests run against a real local Postgres: `<database>_test` on the server in T212_DATABASE_URL
(override with T212_TEST_DATABASE_URL). Start one with `docker compose up -d db`."""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from t212_mcp import db
from t212_mcp.config import DatabaseSettings
from t212_mcp.db.models import Base


def _test_url() -> str:
    if url := os.environ.get("T212_TEST_DATABASE_URL"):
        return url
    url = make_url(DatabaseSettings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _database() -> str:
    url = make_url(_test_url())
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": url.database}):
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as e:
        pytest.exit(f"Postgres not reachable at {url.render_as_string()} ({type(e).__name__}). "
                    "Start it with `docker compose up -d db`.", returncode=1)
    finally:
        admin.dispose()
    rendered = url.render_as_string(hide_password=False)
    fresh = create_engine(rendered)
    with fresh.begin() as conn:  # drop tables left by an older schema; db.engine() recreates them
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    fresh.dispose()
    return rendered


@pytest.fixture
def database_url(_database) -> str:
    with db.engine(_database).begin() as conn:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return _database
