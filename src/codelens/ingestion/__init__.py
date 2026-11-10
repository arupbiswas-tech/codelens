"""Repository ingestion: clone, walk, chunk."""

from codelens.ingestion.models import Chunk, IngestResult
from codelens.ingestion.pipeline import ingest_directory, ingest_repo

__all__ = ["Chunk", "IngestResult", "ingest_directory", "ingest_repo"]
