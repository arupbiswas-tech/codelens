"""Test helpers shared by the database-backed tests."""

import zlib

from codelens.errors import EmbeddingError
from codelens.ingestion.models import IngestResult, build_chunk


class FakeEmbedder:
    """A bag-of-words embedder: texts that share words get similar vectors.

    Real enough for ranking tests (no model download), and it records what it embedded.
    """

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.embedded_texts: list[str] = []

    def embed(self, texts):
        if self.fail:
            raise EmbeddingError("model down")
        self.embedded_texts.extend(texts)
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * 384
        for word in text.lower().replace("_", " ").replace(".", " ").split():
            vector[zlib.crc32(word.encode()) % 384] += 1.0
        norm = sum(x * x for x in vector) ** 0.5 or 1.0
        return [x / norm for x in vector]


def make_result(*specs: tuple[str, str | None, str, int], name: str = "o/r") -> IngestResult:
    """Each spec is (path, symbol, code, start_line); the chunk is one line long."""
    chunks = [
        build_chunk(path, [""] * (start - 1) + [code], start, start, "function", symbol)
        for path, symbol, code, start in specs
    ]
    return IngestResult(name, f"https://github.com/{name}", "c" * 40, chunks, {})
