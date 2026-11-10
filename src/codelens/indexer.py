"""Index a repo end to end: clone -> chunk -> embed -> store."""

import logging
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from codelens.embeddings import Embedder
from codelens.errors import RepoTooLargeError
from codelens.ingestion.pipeline import ingest_repo
from codelens.store import SaveSummary, save_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    repo: str
    commit_sha: str
    summary: SaveSummary


def index_repo(
    url: str,
    pool: ConnectionPool,
    embedder: Embedder,
    max_chunks: int,
    clone_timeout: float,
) -> IndexResult:
    """Ingest a GitHub repo and store it. Safe to call again: only changes are re-embedded."""
    result = ingest_repo(url, clone_timeout=clone_timeout)

    # Check before embedding, which is the expensive step.
    if len(result.chunks) > max_chunks:
        raise RepoTooLargeError(
            f"{result.repo_name} has {len(result.chunks)} chunks; the limit is {max_chunks}."
        )

    summary = save_repo(pool, embedder, result)
    logger.info("Indexed %s at %s: %s", result.repo_name, result.commit_sha[:8], summary)
    return IndexResult(repo=result.repo_name, commit_sha=result.commit_sha, summary=summary)
