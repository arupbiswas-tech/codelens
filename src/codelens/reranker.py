"""Re-order search hits with a cross-encoder, which judges question and chunk together.

The vector search compares the question and each chunk separately (fast, but approximate).
A cross-encoder reads both at once, so it is much more precise but far too slow to run on
every chunk. That is why it only sees the ~20 candidates the searches already picked.
"""

import threading
import time
from collections.abc import Callable

from fastembed.rerank.cross_encoder import TextCrossEncoder

from codelens.errors import RerankError
from codelens.retrieval import Hit
from codelens.retry import run_with_retry

BATCH_SIZE = 8  # fastest of 4/8/16/64 in testing on variable-length code chunks


class Reranker:
    def __init__(self, model_name: str, sleep: Callable[[float], None] = time.sleep):
        self._sleep = sleep
        self._lock = threading.Lock()  # one rerank at a time; the model already uses all cores
        try:
            self._model = TextCrossEncoder(model_name=model_name)
        except Exception as exc:  # unknown name, missing files, no network for a first download
            raise RerankError(f"Could not load reranker model '{model_name}': {exc}") from exc

    def rerank(self, question: str, hits: list[Hit], top_n: int) -> list[Hit]:
        """Return the `top_n` hits the model finds most relevant, best first."""
        if not hits:
            return []

        # Same padding problem as in embeddings.py: a batch is padded to its longest text.
        # Scoring the shortest chunks first keeps batches uniform (measured: 5x faster,
        # identical scores). Order does not matter here because we sort by score afterwards.
        shortest_first = sorted(hits, key=lambda hit: len(hit.text))

        def call() -> list[float]:
            with self._lock:
                scores = self._model.rerank(
                    question, [hit.text for hit in shortest_first], batch_size=BATCH_SIZE
                )
                return [float(score) for score in scores]

        scores = run_with_retry(
            call,
            give_up_error=lambda: RerankError("The reranking model failed."),
            what="Reranking",
            sleep=self._sleep,
        )
        scored = zip(shortest_first, scores, strict=True)
        best_first = sorted(scored, key=lambda pair: pair[1], reverse=True)
        return [hit for hit, _score in best_first[:top_n]]
