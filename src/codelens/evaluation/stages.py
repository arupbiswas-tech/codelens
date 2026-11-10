"""Run each retrieval stage separately, so we can measure what every stage adds."""

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from codelens.embeddings import Embedder
from codelens.reranker import Reranker
from codelens.retrieval import (
    RRF_K,
    Hit,
    keyword_search,
    reciprocal_rank_fusion,
    vector_search,
)

DEPTH = 10  # how many results per stage we keep for hit@k

# The order shown in the results table, weakest to strongest.
STAGES = ("vector", "keyword", "hybrid", "hybrid+rerank")


@dataclass(frozen=True)
class RetrievalConfig:
    """The numbers that /ask uses (see Settings), changeable for tuning experiments."""

    per_search: int = 30  # results taken from each of the two searches
    fused: int = 20  # candidates kept after merging, and handed to the reranker
    rrf_k: int = RRF_K


def run_stages(
    pool: ConnectionPool,
    embedder: Embedder,
    reranker: Reranker,
    repo_id: int,
    question: str,
    config: RetrievalConfig,
) -> dict[str, list[Hit]]:
    """Return the ranked hits of every stage for one question.

    "pool" is the full merged candidate list before reranking: its hit rate is the ceiling
    for what the reranker can possibly achieve.
    """
    question_vector = embedder.embed([question])[0]
    with pool.connection() as conn:
        by_meaning = vector_search(conn, repo_id, question_vector, config.per_search)
        by_words = keyword_search(conn, repo_id, question, config.per_search)

    merged = reciprocal_rank_fusion([by_meaning, by_words], config.rrf_k)[: config.fused]
    return {
        "vector": by_meaning[:DEPTH],
        "keyword": by_words[:DEPTH],
        "hybrid": merged[:DEPTH],
        "hybrid+rerank": reranker.rerank(question, merged, top_n=DEPTH),
        "pool": merged,
    }
