"""Run the evaluation:  python -m codelens.evaluation [--no-answers] [--tune]

Needs the database and (unless --no-answers) GROQ_API_KEY. Use `make eval` / `make eval-tune`.
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from codelens.config import get_settings
from codelens.db import create_pool
from codelens.embeddings import Embedder
from codelens.errors import CodeLensError
from codelens.evaluation import report
from codelens.evaluation.dataset import DatasetError, load_dataset
from codelens.evaluation.runner import (
    AnswerRecord,
    Context,
    check_labels,
    index_dataset_repos,
    load_chunks,
    run_answers,
    run_retrieval,
    summarize_answers,
    summarize_retrieval,
)
from codelens.evaluation.stages import RetrievalConfig
from codelens.evaluation.tune import tune_chunking, tune_search
from codelens.ingestion import chunker
from codelens.llm import LLMClient
from codelens.logging_setup import setup_logging
from codelens.reranker import Reranker

logger = logging.getLogger(__name__)


def build_llm(settings, model: str) -> LLMClient | None:
    """An LLM client for `model`, or None when no API key is configured."""
    key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else ""
    if not key:
        return None
    return LLMClient(
        key, model, settings.groq_base_url, settings.llm_timeout, settings.llm_max_retries
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate CodeLens retrieval and answers.")
    parser.add_argument("--dataset", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--out", type=Path, default=Path("eval/results"))
    parser.add_argument("--no-answers", action="store_true", help="retrieval only: no LLM calls")
    parser.add_argument("--tune", action="store_true", help="also run the tuning experiments")
    parser.add_argument(
        "--no-rerank", action="store_true", help="answer from the merged search order (A/B test)"
    )
    parser.add_argument(
        "--resume",
        type=Path,
        help="results JSON of an interrupted run: keep its good answers, redo the failed ones",
    )
    parser.add_argument(
        "--pause", type=float, default=0.0, help="seconds to wait between LLM questions"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2

    try:
        previous = load_previous_answers(args.resume, rerank=not args.no_rerank)
    except DatasetError as exc:
        print(f"resume error: {exc}", file=sys.stderr)
        return 2

    llm = None if args.no_answers else build_llm(settings, settings.groq_model)
    if not args.no_answers and llm is None:
        print("GROQ_API_KEY is not set. Set it, or run with --no-answers.", file=sys.stderr)
        return 2
    judge_llm = llm
    if llm is not None and settings.eval_judge_model:
        judge_llm = build_llm(settings, settings.eval_judge_model)

    pool = create_pool(settings)
    try:
        ctx = Context(
            pool=pool,
            embedder=Embedder(
                settings.embedding_model, settings.embedding_dim, settings.embedding_batch_size
            ),
            reranker=Reranker(settings.reranker_model),
            settings=settings,
            llm=llm,
            judge_llm=judge_llm,
        )
        return evaluate(ctx, dataset, args, previous)
    except CodeLensError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        pool.close()


def load_previous_answers(path: Path | None, rerank: bool) -> list[AnswerRecord] | None:
    """Read the answer records of an earlier run, if --resume was given."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read {path}: {exc}") from exc
    if data.get("config", {}).get("reranker_used_for_answers", True) != rerank:
        raise DatasetError(f"{path} was produced {'without' if rerank else 'with'} the reranker.")
    return [AnswerRecord(**record) for record in data.get("answer_records", [])]


def evaluate(ctx: Context, dataset, args, previous: list[AnswerRecord] | None) -> int:
    settings = ctx.settings
    print(f"Indexing {len(dataset.repos)} repos (unchanged ones are reused)...")
    repo_ids, warnings = index_dataset_repos(ctx, dataset)
    for warning in warnings:
        print(f"WARNING: {warning}")

    chunks_by_repo = {repo: load_chunks(ctx.pool, repo_id) for repo, repo_id in repo_ids.items()}
    problems = check_labels(dataset, chunks_by_repo)
    if problems:
        print(
            "The dataset labels do not match the indexed code:",
            *problems,
            sep="\n  ",
            file=sys.stderr,
        )
        return 2

    config = RetrievalConfig(settings.search_candidates, settings.fused_candidates)
    n = len(dataset.answerable())
    ranks = run_retrieval(ctx, dataset, repo_ids, config)
    retrieval = summarize_retrieval(ranks, config.fused)
    sections = [
        f"## Retrieval ({n} answerable questions, {len(dataset.repos)} repos)\n",
        report.retrieval_table(retrieval, n, config.fused),
        "\nQuestions the final pipeline did not place in the top 3:\n",
        report.misses(dataset, ranks["hybrid+rerank"]),
    ]
    results: dict = {"retrieval": retrieval, "retrieval_ranks": ranks}

    if ctx.llm is not None:
        records = run_answers(
            ctx,
            dataset,
            chunks_by_repo,
            rerank=not args.no_rerank,
            pause=args.pause,
            previous=previous,
        )
        answers = summarize_answers(records)
        results["answers"] = answers
        results["answer_records"] = [asdict(r) for r in records]
        sections.append(f"\n## Answers ({len(dataset.questions)} questions)\n")
        incomplete = report.incomplete_warning(answers, len(records), len(dataset.questions))
        if incomplete:
            sections.append(incomplete + "\n")
        sections.append(report.answers_table(answers))

    if args.tune:
        chunking = tune_chunking(ctx, dataset, repo_ids)
        search = tune_search(ctx, dataset, repo_ids)
        results["tuning"] = {"chunking": chunking, "search": search}
        sections += [
            "\n## Tuning: chunk size\n",
            report.tuning_table(chunking, n, ["chunks", "setting"]),
            "\n## Tuning: candidates and RRF constant\n",
            report.tuning_table(search, n, []),
        ]

    results["config"] = {
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
        "llm_model": settings.groq_model,
        "judge_model": settings.eval_judge_model or settings.groq_model,
        "search_candidates": config.per_search,
        "fused_candidates": config.fused,
        "rrf_k": config.rrf_k,
        "context_chunks": settings.context_chunks,
        "reranker_used_for_answers": not args.no_rerank,
        "max_chunk_lines": chunker.MAX_CHUNK_LINES,
        "window_lines": chunker.WINDOW_LINES,
        "window_overlap": chunker.WINDOW_OVERLAP,
        "commits": {repo: info.commit for repo, info in dataset.repos.items()},
    }
    write_results(args.out, results, "\n".join(sections), latest_name(args))
    print("\n" + "\n".join(sections))
    return 0


def latest_name(args) -> str:
    """Each kind of run gets its own 'latest' file, so one run never overwrites another."""
    if args.no_answers:
        return "latest-retrieval.md"
    return "latest-no-rerank.md" if args.no_rerank else "latest.md"


def write_results(out_dir: Path, results: dict, markdown: str, latest: str) -> None:
    """Save the full results (JSON, one file per run) and the latest tables (Markdown)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out_dir / latest).write_text(markdown + "\n", encoding="utf-8")
    print(f"\nSaved {out_dir / (stamp + '.json')} and {out_dir / latest}")


if __name__ == "__main__":
    sys.exit(main())
