"""The ingestion pipeline: clone a repo, walk its files, chunk them."""

import logging
import tempfile
from collections import Counter
from pathlib import Path

from codelens.ingestion.chunker import chunk_file
from codelens.ingestion.cloner import clone_repo, parse_repo_url
from codelens.ingestion.models import Chunk, IngestResult
from codelens.ingestion.walker import walk_repo

logger = logging.getLogger(__name__)


def ingest_repo(url: str, clone_timeout: float = 120) -> IngestResult:
    """Clone a GitHub repo into a temporary folder and turn it into chunks.

    Raises InvalidInputError for a bad URL and RepoCloneError if git fails.
    The temporary clone is always deleted, even on errors.
    """
    name, clean_url = parse_repo_url(url)
    logger.info("Ingesting %s", name)

    with tempfile.TemporaryDirectory(prefix="codelens-") as tmp:
        clone_path = Path(tmp) / "repo"
        commit_sha = clone_repo(clean_url, clone_path, timeout=clone_timeout)
        chunks, stats = ingest_directory(clone_path)

    logger.info("Ingested %s at %s: %d chunks, %s", name, commit_sha[:8], len(chunks), dict(stats))
    return IngestResult(
        repo_name=name, url=clean_url, commit_sha=commit_sha, chunks=chunks, stats=dict(stats)
    )


def ingest_directory(root: Path) -> tuple[list[Chunk], Counter]:
    """Chunk every indexable file under `root`. One bad file never stops the run."""
    stats: Counter = Counter()
    chunks: list[Chunk] = []

    for source in walk_repo(root, stats):
        try:
            file_chunks = chunk_file(source.path, source.text)
        except Exception:  # a bug in one file must not lose the whole repo
            logger.exception("Chunking failed for %s; skipping it.", source.path)
            stats["failed_chunking"] += 1
            continue
        chunks.extend(file_chunks)

    stats["chunks"] = len(chunks)
    return chunks, stats
