"""Turn text into vectors with a local model (fastembed, ONNX, no API key or cost)."""

import logging
import threading
import time
from collections.abc import Callable, Sequence

from fastembed import TextEmbedding

from codelens.errors import EmbeddingError
from codelens.retry import run_with_retry

logger = logging.getLogger(__name__)


class Embedder:
    """Wraps the model so the rest of the app only sees `embed(texts) -> vectors`."""

    def __init__(
        self,
        model_name: str,
        expected_dim: int,
        batch_size: int,
        sleep: Callable[[float], None] = time.sleep,  # replaced in tests so they run fast
    ):
        self._batch_size = batch_size
        self._sleep = sleep
        # The model already uses every CPU core for one call. Letting two requests embed at
        # once would only make both slower, so we take turns.
        self._lock = threading.Lock()
        self._model = self._load(model_name)
        self._check_dimension(expected_dim)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in batches; results come back in the same order as `texts`.

        Raises EmbeddingError if a batch keeps failing.
        """
        # A batch is padded to its longest text, so mixing a 3000-character function with
        # 100-character snippets wastes most of the work. Sorting by length first keeps
        # similar sizes together (measured: about 6x faster on a real repo).
        shortest_first = sorted(range(len(texts)), key=lambda i: len(texts[i]))

        vectors: list[list[float]] = [[] for _ in texts]
        for start in range(0, len(texts), self._batch_size):
            positions = shortest_first[start : start + self._batch_size]
            batch_vectors = self._embed_batch_with_retry([texts[i] for i in positions])
            for position, vector in zip(positions, batch_vectors, strict=True):
                vectors[position] = vector
        return vectors

    def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        def call() -> list[list[float]]:
            with self._lock:
                return [vector.tolist() for vector in self._model.embed(batch)]

        return run_with_retry(
            call,
            give_up_error=lambda: EmbeddingError("The embedding model failed; please try again."),
            what="Embedding batch",
            sleep=self._sleep,
        )

    @staticmethod
    def _load(model_name: str) -> TextEmbedding:
        """Load the model. The Docker image has it pre-downloaded, so this works offline."""
        try:
            return TextEmbedding(model_name=model_name)
        except Exception as exc:  # unknown name, missing files, no network for a first download
            raise EmbeddingError(f"Could not load embedding model '{model_name}': {exc}") from exc

    def _check_dimension(self, expected_dim: int) -> None:
        """Fail at startup, not on the first insert, if the model does not match the schema."""
        actual_dim = len(self.embed(["dimension check"])[0])
        if actual_dim != expected_dim:
            raise EmbeddingError(
                f"Embedding model produces {actual_dim}-dim vectors but the database column "
                f"is vector({expected_dim}). Change the model or migrate the schema."
            )
