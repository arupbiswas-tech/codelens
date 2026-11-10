"""Shared test setup and the real-database fixtures."""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

# Settings require DATABASE_URL. Unit tests never connect to it; the database-backed tests
# use TEST_DATABASE_URL instead.
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
INIT_SQL = Path(__file__).resolve().parent.parent / "db" / "init.sql"


@pytest.fixture(scope="session")
def _schema_pool():
    """One pool for the whole run, working inside its own throwaway Postgres schema."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    # "public" stays on the path so the `vector` type from the extension is found.
    options = f"-c search_path={schema},public"
    pool = ConnectionPool(
        TEST_DATABASE_URL, kwargs={"options": options}, min_size=1, max_size=3, open=True
    )
    with pool.connection() as conn:
        conn.execute(INIT_SQL.read_text())
    yield pool
    pool.close()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pool(_schema_pool):
    """The test pool with empty tables. Skips the test when no database is configured."""
    with _schema_pool.connection() as conn:
        conn.execute("TRUNCATE repos CASCADE")
    return _schema_pool
