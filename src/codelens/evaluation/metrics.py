"""Retrieval metrics. Pure functions: no database, no models."""

from collections.abc import Sequence

from codelens.evaluation.dataset import Label
from codelens.retrieval import Hit


def label_matches(hit: Hit, label: Label) -> bool:
    """Is this chunk the place the label points to?"""
    if hit.path != label.path:
        return False
    if label.symbol is not None:
        return hit.symbol == label.symbol
    first, last = label.lines
    return hit.start_line <= last and hit.end_line >= first  # the ranges overlap


def is_relevant(hit: Hit, labels: Sequence[Label]) -> bool:
    """A chunk is relevant if it matches ANY of the question's alternative labels."""
    return any(label_matches(hit, label) for label in labels)


def first_relevant_rank(hits: Sequence[Hit], labels: Sequence[Label]) -> int | None:
    """1-based position of the first relevant chunk, or None if there is none."""
    for rank, hit in enumerate(hits, start=1):
        if is_relevant(hit, labels):
            return rank
    return None


def summarize_ranks(ranks: Sequence[int | None], ks: Sequence[int]) -> dict[str, float]:
    """Aggregate the first-relevant ranks of many questions.

    hit@k  = share of questions whose answer chunk is in the top k results.
    mrr    = mean of 1/rank (0 when not found): rewards putting the answer near the top.
    """
    total = len(ranks)
    if total == 0:
        return {}
    summary = {f"hit@{k}": sum(r is not None and r <= k for r in ranks) / total for k in ks}
    summary["mrr"] = sum(1 / r for r in ranks if r) / total
    return summary
