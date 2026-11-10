"""Tuning experiments: try other chunk sizes and search settings, measure retrieval only.

No LLM is involved, so these runs are free and repeatable. Each experiment changes ONE
group of numbers and reports hit@k / MRR for the hybrid and hybrid+rerank pipelines.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from codelens.evaluation.dataset import Dataset
from codelens.evaluation.runner import (
    Context,
    check_labels,
    delete_aliases,
    index_dataset_repos,
    load_chunks,
    run_retrieval,
    summarize_retrieval,
)
from codelens.evaluation.stages import RetrievalConfig
from codelens.ingestion import chunker

logger = logging.getLogger(__name__)

# name -> (MAX_CHUNK_LINES, WINDOW_LINES, WINDOW_OVERLAP). "current" is the production setting.
CHUNK_CONFIGS = {
    "small": (60, 30, 5),
    "current": (chunker.MAX_CHUNK_LINES, chunker.WINDOW_LINES, chunker.WINDOW_OVERLAP),
    "large": (200, 100, 20),
}

# (candidates per search, candidates kept after merging) and the RRF constant k.
CANDIDATE_CONFIGS = [(10, 10), (30, 20), (50, 40)]
RRF_KS = [10, 60, 100]


@contextmanager
def chunk_settings(max_lines: int, window: int, overlap: int) -> Iterator[None]:
    """Temporarily change the chunker's size constants, then always restore them."""
    saved = (chunker.MAX_CHUNK_LINES, chunker.WINDOW_LINES, chunker.WINDOW_OVERLAP)
    chunker.MAX_CHUNK_LINES, chunker.WINDOW_LINES, chunker.WINDOW_OVERLAP = (
        max_lines,
        window,
        overlap,
    )
    try:
        yield
    finally:
        chunker.MAX_CHUNK_LINES, chunker.WINDOW_LINES, chunker.WINDOW_OVERLAP = saved


def _row(name: str, summary: dict[str, dict[str, float]], **extra) -> dict:
    """One result row: the hybrid and hybrid+rerank numbers of an experiment."""
    return {
        "name": name,
        **extra,
        "hybrid": summary["hybrid"],
        "hybrid+rerank": summary["hybrid+rerank"],
    }


def tune_chunking(ctx: Context, dataset: Dataset, primary_repo_ids: dict[str, int]) -> list[dict]:
    """Re-index every repo with other chunk sizes and measure retrieval on each."""
    rows = []
    config = RetrievalConfig(ctx.settings.search_candidates, ctx.settings.fused_candidates)
    try:
        for name, (max_lines, window, overlap) in CHUNK_CONFIGS.items():
            logger.info("Chunking experiment '%s' (%d/%d/%d)", name, max_lines, window, overlap)
            if name == "current":
                repo_ids = primary_repo_ids  # already indexed with these numbers
            else:
                with chunk_settings(max_lines, window, overlap):
                    repo_ids, _ = index_dataset_repos(ctx, dataset, alias_tag=name)

            chunks = {repo: load_chunks(ctx.pool, repo_id) for repo, repo_id in repo_ids.items()}
            ranks = run_retrieval(ctx, dataset, repo_ids, config)
            rows.append(
                _row(
                    name,
                    summarize_retrieval(ranks, config.fused),
                    chunks=sum(len(c) for c in chunks.values()),
                    label_problems=len(check_labels(dataset, chunks)),
                    setting=f"max {max_lines} / window {window} / overlap {overlap}",
                )
            )
    finally:
        delete_aliases(ctx.pool)  # never leave experiment copies in the database
    return rows


def tune_search(ctx: Context, dataset: Dataset, repo_ids: dict[str, int]) -> list[dict]:
    """Try other candidate counts and RRF constants on the normal index."""
    rows = []
    for per_search, fused in CANDIDATE_CONFIGS:
        for rrf_k in RRF_KS:
            logger.info(
                "Search experiment: per_search=%d fused=%d rrf_k=%d", per_search, fused, rrf_k
            )
            config = RetrievalConfig(per_search, fused, rrf_k)
            ranks = run_retrieval(ctx, dataset, repo_ids, config)
            rows.append(
                _row(
                    f"{per_search}/{fused}, k={rrf_k}",
                    summarize_retrieval(ranks, fused),
                    setting=f"per_search {per_search}, fused {fused}, rrf_k {rrf_k}",
                )
            )
    return rows
