"""Format evaluation results as the Markdown tables used in the README."""

from codelens.evaluation.dataset import Dataset
from codelens.evaluation.runner import KS
from codelens.evaluation.stages import STAGES

STAGE_LABELS = {
    "vector": "vector search only",
    "keyword": "keyword search only",
    "hybrid": "hybrid (vector + keyword, RRF)",
    "hybrid+rerank": "hybrid + reranker",
}


def _count(fraction: float, n: int) -> str:
    """Show 'x/n' instead of a percentage: with ~25 questions a percentage looks too exact."""
    return f"{round(fraction * n)}/{n}"


def retrieval_table(summary: dict[str, dict[str, float]], n: int, pool_size: int) -> str:
    """Markdown table: one row per retrieval stage, hit@k as counts plus MRR."""
    header = "| Pipeline | " + " | ".join(f"hit@{k}" for k in KS) + " | MRR |"
    lines = [header, "|---|" + "---|" * (len(KS) + 1)]
    for stage in STAGES:
        row = summary[stage]
        cells = [_count(row[f"hit@{k}"], n) for k in KS] + [f"{row['mrr']:.2f}"]
        lines.append(f"| {STAGE_LABELS[stage]} | " + " | ".join(cells) + " |")
    pool_hit = summary["pool"][f"hit@{pool_size}"]
    lines.append(
        f"\nThe merged candidate pool (top {pool_size}, before reranking) contains the answer "
        f"for {_count(pool_hit, n)} questions: the reranker cannot do better than that."
    )
    return "\n".join(lines)


def answers_table(summary: dict) -> str:
    """Markdown table of the answer-quality counts."""
    n, u = summary["answerable"], summary["unanswerable"]
    rows = [
        ("Answers with at least one verified citation", f"{summary['with_citation']}/{n}"),
        ("Answers citing the labelled code", f"{summary['correct_citation']}/{n}"),
        ("Made-up citations (dropped, never shown)", str(summary["ungrounded_citations"])),
        ("Faithfulness (claims supported by the shown code, mean)", str(summary["faithfulness"])),
        ("Judged correct / partly correct / wrong",
         f"{summary['correct_yes']} / {summary['correct_partial']} / {summary['correct_no']}"
         f" (of {summary['judged']} judged)"),
        ("Wrongly refused to answer", f"{summary['false_refusals']}/{n}"),
        ("Correctly refused an unanswerable question", f"{summary['correct_refusals']}/{u}"),
        ("Questions that failed (not scored)", str(summary["errors"])),
        ("Median time per question", f"{summary['latency_p50_s']} s"),
    ]  # fmt: skip
    return "| Measure | Result |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)


def incomplete_warning(summary: dict, recorded: int, total: int) -> str:
    """A loud note when some questions failed or were never asked, else an empty string."""
    if not summary["errors"] and recorded == total:
        return ""
    return (
        f"> **INCOMPLETE:** {summary['errors']} question(s) failed and {total - recorded} were "
        "never asked (for example because the LLM quota ran out). The numbers below cover "
        "only the questions that were answered. Re-run with `--resume <results.json>` to fill "
        "in the rest."
    )


def tuning_table(rows: list[dict], n: int, extra_columns: list[str]) -> str:
    """Markdown table for a tuning experiment (hybrid+rerank, with hybrid MRR for reference)."""
    header = ["Setting", *extra_columns, "hit@1", "hit@3", "hit@6", "MRR", "MRR without reranker"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        best, plain = row["hybrid+rerank"], row["hybrid"]
        cells = [row["name"], *(str(row[c]) for c in extra_columns),
                 _count(best["hit@1"], n), _count(best["hit@3"], n), _count(best["hit@6"], n),
                 f"{best['mrr']:.2f}", f"{plain['mrr']:.2f}"]  # fmt: skip
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def misses(dataset: Dataset, ranks: dict[str, int | None]) -> str:
    """List the questions the final pipeline did not answer at rank 1-3: where to look next."""
    lines = []
    for question in dataset.answerable():
        rank = ranks.get(question.id)
        if rank is None or rank > 3:
            where = "not found in top 10" if rank is None else f"found at rank {rank}"
            lines.append(f"- `{question.id}` ({question.category}): {question.question} -> {where}")
    return "\n".join(lines) or "- none"
