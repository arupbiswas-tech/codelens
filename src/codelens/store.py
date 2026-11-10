"""Save an ingested repo to Postgres: embed only what changed, replace what went stale."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

from codelens.db import translate_db_errors
from codelens.embeddings import Embedder
from codelens.ingestion.models import Chunk, IngestResult

logger = logging.getLogger(__name__)

# A stored embedding is only reusable if all three of these are unchanged, because the
# embedded text is built from path + symbol + content (see Chunk.embedding_text).
ReuseKey = tuple[str, str | None, str]  # (path, symbol, content_hash)

LOAD_EXISTING_SQL = """
    SELECT c.path, c.symbol, c.content_hash, c.embedding::text
    FROM chunks c JOIN repos r ON r.id = c.repo_id
    WHERE r.name = %s AND c.embedding IS NOT NULL
"""

UPSERT_REPO_SQL = """
    INSERT INTO repos (name, url, commit_sha) VALUES (%s, %s, %s)
    ON CONFLICT (name) DO UPDATE
        SET url = EXCLUDED.url, commit_sha = EXCLUDED.commit_sha, ingested_at = now()
    RETURNING id
"""

# DO NOTHING: a chunk already stored at the same place with the same content stays untouched.
INSERT_CHUNK_SQL = """
    INSERT INTO chunks
        (repo_id, path, symbol, kind, start_line, end_line, content, content_hash, embedding)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
    ON CONFLICT (repo_id, path, start_line, content_hash) DO NOTHING
"""

# Remove chunks of this repo that are not in the new ingest (edited, moved or deleted code).
DELETE_STALE_SQL = """
    DELETE FROM chunks
    WHERE repo_id = %s
      AND (path, start_line, content_hash) NOT IN (
          SELECT * FROM unnest(%s::text[], %s::int[], %s::text[])
      )
"""


@dataclass(frozen=True)
class SaveSummary:
    chunks: int  # chunks in the repo now
    embedded: int  # chunks that went through the model this time
    reused: int  # chunks whose stored embedding was kept
    deleted: int  # stale chunks removed


def save_repo(pool: ConnectionPool, embedder: Embedder, result: IngestResult) -> SaveSummary:
    """Store `result` so the database matches the repo exactly.

    The slow part (embedding) happens before the write transaction opens, so no database
    connection is held while the CPU works. The write itself is one transaction: it either
    fully applies or not at all, and searches never see a half-updated repo.
    """
    with translate_db_errors(f"saving {result.repo_name}"):
        existing = _load_existing_embeddings(pool, result.repo_name)
        vectors, embedded = _vectors_for(result.chunks, existing, embedder)
        deleted = _write_repo(pool, result, vectors)

    return SaveSummary(
        chunks=len(result.chunks),
        embedded=embedded,
        reused=len(result.chunks) - embedded,
        deleted=deleted,
    )


def _reuse_key(chunk: Chunk) -> ReuseKey:
    return (chunk.path, chunk.symbol, chunk.content_hash)


def _load_existing_embeddings(pool: ConnectionPool, repo_name: str) -> dict[ReuseKey, str]:
    """Embeddings already stored for this repo, as pgvector text like '[0.1,0.2,...]'."""
    with pool.connection() as conn:
        rows = conn.execute(LOAD_EXISTING_SQL, (repo_name,)).fetchall()
    return {(path, symbol, content_hash): vector for path, symbol, content_hash, vector in rows}


def _vectors_for(
    chunks: list[Chunk], existing: dict[ReuseKey, str], embedder: Embedder
) -> tuple[list[str], int]:
    """Return one pgvector-text vector per chunk (same order) and how many were newly embedded."""
    # Keyed by reuse key, so identical code in two places is embedded only once.
    missing = {_reuse_key(c): c for c in chunks if _reuse_key(c) not in existing}
    new_vectors = embedder.embed([c.embedding_text for c in missing.values()])
    new_by_key = {key: _to_pgvector(vec) for key, vec in zip(missing, new_vectors, strict=True)}
    known = existing | new_by_key
    return [known[_reuse_key(c)] for c in chunks], len(missing)


def _to_pgvector(vector: list[float]) -> str:
    """pgvector accepts '[1,2,3]' text, and a JSON array has exactly that shape."""
    return json.dumps(vector)


def _write_repo(pool: ConnectionPool, result: IngestResult, vectors: list[str]) -> int:
    """Upsert the repo, insert its chunks, delete stale ones. Returns the number deleted."""
    chunk_rows = [
        (c.path, c.symbol, c.kind, c.start_line, c.end_line, c.content, c.content_hash, vec)
        for c, vec in zip(result.chunks, vectors, strict=True)
    ]
    with pool.connection() as conn:  # commits on success, rolls back if anything raises
        # Two ingests of the same repo at once would delete each other's rows. The lock makes
        # the second wait; it is released automatically when this transaction ends.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (result.repo_name,))
        repo_id = conn.execute(
            UPSERT_REPO_SQL, (result.repo_name, result.url, result.commit_sha)
        ).fetchone()[0]

        with conn.cursor() as cursor:
            cursor.executemany(INSERT_CHUNK_SQL, [(repo_id, *row) for row in chunk_rows])
            cursor.execute(
                DELETE_STALE_SQL,
                (
                    repo_id,
                    [c.path for c in result.chunks],
                    [c.start_line for c in result.chunks],
                    [c.content_hash for c in result.chunks],
                ),
            )
            return cursor.rowcount


LIST_REPOS_SQL = """
    SELECT r.name, r.url, r.commit_sha, r.ingested_at, count(c.id)
    FROM repos r LEFT JOIN chunks c ON c.repo_id = r.id
    WHERE r.name NOT LIKE '%@eval-%'
    GROUP BY r.id
    ORDER BY r.name
"""


@dataclass(frozen=True)
class RepoSummary:
    name: str
    url: str
    commit_sha: str | None
    ingested_at: datetime
    chunks: int


def list_repos(pool: ConnectionPool) -> list[RepoSummary]:
    """Every indexed repo with its chunk count. Temporary evaluation copies are hidden."""
    with translate_db_errors("listing repos"), pool.connection() as conn:
        rows = conn.execute(LIST_REPOS_SQL).fetchall()
    return [RepoSummary(*row) for row in rows]
