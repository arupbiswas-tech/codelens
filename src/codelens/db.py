"""Database access: connection pool creation and a readiness check."""

import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from codelens.config import Settings
from codelens.errors import DatabaseUnavailableError

logger = logging.getLogger(__name__)


def create_pool(settings: Settings) -> ConnectionPool:
    """Create the pool and open it. Raises if the database cannot be reached in time."""
    pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_connect_timeout,
        kwargs={
            # connect_timeout takes whole seconds, and 0 would mean "wait forever".
            "connect_timeout": max(1, math.ceil(settings.db_connect_timeout)),
            "options": f"-c statement_timeout={int(settings.db_statement_timeout * 1000)}",
        },
        open=False,
    )
    try:
        pool.open(wait=True, timeout=settings.db_connect_timeout)
    except PoolTimeout as exc:
        pool.close()
        raise DatabaseUnavailableError(
            f"Could not connect to the database within {settings.db_connect_timeout}s."
        ) from exc
    return pool


def check_database(pool: ConnectionPool) -> None:
    """Confirm the database answers and the pgvector extension is installed.

    Raises DatabaseUnavailableError with a readable message if anything is wrong.
    """
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
            row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    except (psycopg.Error, PoolTimeout) as exc:
        logger.error("Database check failed: %s", exc)
        raise DatabaseUnavailableError("Database is not reachable.") from exc

    if row is None:
        raise DatabaseUnavailableError("The pgvector extension is not installed in this database.")


@contextmanager
def translate_db_errors(action: str) -> Iterator[None]:
    """Turn "database down / pool busy / query too slow" into a clean 503 for the client.

    Other database errors (bad SQL, constraint violations) are bugs, so they are not
    caught here: they surface as a generic 500 and a full traceback in our logs.
    """
    try:
        yield
    except (psycopg.OperationalError, PoolTimeout) as exc:
        logger.error("Database error while %s: %s", action, exc)
        raise DatabaseUnavailableError("The database is unavailable or too slow.") from exc
