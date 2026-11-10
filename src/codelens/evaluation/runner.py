"""Run the evaluation: index the repos, check the labels, measure retrieval and answers."""

import logging
import statistics
import time
from dataclasses import asdict, dataclass, field

from psycopg_pool import ConnectionPool

from codelens.answering import NOT_FOUND_ANSWER, Answerer
from codelens.config import Settings
from codelens.embeddings import Embedder
from codelens.errors import CodeLensError
from codelens.evaluation.dataset import Dataset, Question
from codelens.evaluation.judge import Judge, Verdict
from codelens.evaluation.metrics import first_relevant_rank, is_relevant, summarize_ranks
from codelens.evaluation.stages import STAGES, RetrievalConfig, run_stages
from codelens.indexer import index_repo
from codelens.ingestion.pipeline import ingest_repo
from codelens.llm import LLMClient
from codelens.reranker import Reranker
from codelens.retrieval import Hit, find_repo_id
from codelens.store import save_repo

logger = logging.getLogger(__name__)

KS = (1, 3, 6, 10)  # the "k" values of hit@k that we report
MAX_CONSECUTIVE_ERRORS = 3  # stop answering after this many failed questions in a row


@dataclass
class Context:
    """The long-lived pieces every evaluation step needs."""

    pool: ConnectionPool
    embedder: Embedder
    reranker: Reranker
    settings: Settings
    llm: LLMClient | None = None  # needed for answer scoring only
    judge_llm: LLMClient | None = None


class LabelError(Exception):
    """A label in the dataset points at code that does not exist in the indexed repo."""


# ---- indexing and label checking -----------------------------------------------------


def index_dataset_repos(
    ctx: Context, dataset: Dataset, alias_tag: str | None = None
) -> tuple[dict[str, int], list[str]]:
    """Index every repo of the dataset. Returns ({repo name: repo id}, warnings).

    With `alias_tag`, each repo is stored under "<name>@eval-<tag>" so tuning experiments
    (different chunk sizes) never overwrite the normal index.
    """
    repo_ids: dict[str, int] = {}
    warnings: list[str] = []
    for name, info in dataset.repos.items():
        if alias_tag is None:
            result = index_repo(
                info.url,
                ctx.pool,
                ctx.embedder,
                ctx.settings.max_chunks_per_repo,
                ctx.settings.clone_timeout,
            )
            commit, stored_name = result.commit_sha, name
        else:
            ingested = ingest_repo(info.url, clone_timeout=ctx.settings.clone_timeout)
            ingested.repo_name = stored_name = f"{name}@eval-{alias_tag}"
            save_repo(ctx.pool, ctx.embedder, ingested)
            commit = ingested.commit_sha
        if commit != info.commit:
            warnings.append(
                f"{name}: labels were written at {info.commit[:8]}, indexed {commit[:8]}"
            )
        repo_ids[name] = find_repo_id(ctx.pool, stored_name)
    return repo_ids, warnings


def delete_aliases(pool: ConnectionPool) -> None:
    """Remove every temporary '@eval-' repo created by tuning (chunks go with them)."""
    with pool.connection() as conn:
        conn.execute("DELETE FROM repos WHERE name LIKE '%@eval-%'")


def load_chunks(pool: ConnectionPool, repo_id: int) -> list[Hit]:
    """Every stored chunk of a repo, used to check labels and to fetch reference code."""
    sql = (
        "SELECT id, path, symbol, kind, start_line, end_line, content "
        "FROM chunks WHERE repo_id = %s"
    )
    with pool.connection() as conn:
        return [Hit(*row) for row in conn.execute(sql, (repo_id,)).fetchall()]


def check_labels(dataset: Dataset, chunks_by_repo: dict[str, list[Hit]]) -> list[str]:
    """Return a problem for every label that matches no chunk (typo, renamed or moved code)."""
    problems = []
    for question in dataset.answerable():
        chunks = chunks_by_repo[question.repo]
        for label in question.expected:
            if not any(is_relevant(chunk, [label]) for chunk in chunks):
                problems.append(f"{question.id}: label {label} matches no chunk")
    return problems


# ---- retrieval ablation --------------------------------------------------------------


def run_retrieval(
    ctx: Context, dataset: Dataset, repo_ids: dict[str, int], config: RetrievalConfig
) -> dict[str, dict[str, int | None]]:
    """For every answerable question, the rank of the first relevant chunk in each stage.

    Returns {stage: {question id: rank or None}}. Stages are those in STAGES plus "pool".
    """
    ranks: dict[str, dict[str, int | None]] = {stage: {} for stage in (*STAGES, "pool")}
    for question in dataset.answerable():
        hits = run_stages(
            ctx.pool, ctx.embedder, ctx.reranker, repo_ids[question.repo], question.question, config
        )
        for stage, stage_hits in hits.items():
            ranks[stage][question.id] = first_relevant_rank(stage_hits, question.expected)
    return ranks


def summarize_retrieval(
    ranks: dict[str, dict[str, int | None]], pool_size: int
) -> dict[str, dict[str, float]]:
    """Turn per-question ranks into hit@k and MRR per stage.

    "pool" is the merged candidate list before reranking, so it only has hit@pool_size.
    """
    summary = {stage: summarize_ranks(list(ranks[stage].values()), KS) for stage in STAGES}
    summary["pool"] = summarize_ranks(list(ranks["pool"].values()), (pool_size,))
    return summary


# ---- answer scoring ------------------------------------------------------------------


@dataclass
class AnswerRecord:
    question_id: str
    answerable: bool
    answer: str
    refused: bool  # the model said it could not find the answer
    citations: list[str]  # verified citations only
    correct_citation: bool  # a verified citation points at a labelled chunk
    ungrounded: int  # citations the model made up
    seconds: float
    verdict: dict | None = None  # judge result, see Verdict
    judge_failed: bool = False
    error: str | None = None  # the question could not be answered at all
    shown: list[str] = field(default_factory=list)  # citations of every chunk the model saw


def run_answers(
    ctx: Context,
    dataset: Dataset,
    chunks_by_repo: dict[str, list[Hit]],
    rerank: bool = True,
    pause: float = 0.0,
    previous: list[AnswerRecord] | None = None,
) -> list[AnswerRecord]:
    """Ask every question through the real /ask pipeline and score the answers.

    `rerank=False` answers from the merged search order instead (an A/B test of the reranker).
    `pause` waits between questions, to stay under the LLM provider's rate limit.
    `previous` are records of an earlier, interrupted run: questions that were answered
    there are kept as they are, so only the failed or missing ones cost LLM calls.

    After MAX_CONSECUTIVE_ERRORS failures in a row the run stops early. That is almost
    always a quota or outage that retrying cannot fix, and continuing would only fill the
    results with errors. Re-run with the saved results as `previous` once it has cleared.
    """
    reranker = ctx.reranker if rerank else None
    answerer = Answerer(ctx.pool, ctx.embedder, reranker, ctx.llm, ctx.settings)
    judge = Judge(ctx.judge_llm or ctx.llm)
    already_done = {r.question_id: r for r in previous or [] if not r.error}

    records: list[AnswerRecord] = []
    errors_in_a_row = 0
    for index, question in enumerate(dataset.questions, start=1):
        if question.id in already_done:
            records.append(already_done[question.id])
            continue
        logger.info("Answering %d/%d: %s", index, len(dataset.questions), question.id)
        record = _answer_one(answerer, judge, question, chunks_by_repo[question.repo])
        records.append(record)

        errors_in_a_row = errors_in_a_row + 1 if record.error else 0
        if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
            logger.error("Stopping early after %d failures in a row.", errors_in_a_row)
            break
        time.sleep(pause)
    return records


def _answer_one(
    answerer: Answerer, judge: Judge, question: Question, chunks: list[Hit]
) -> AnswerRecord:
    started = time.perf_counter()
    try:
        answer = answerer.ask(question.repo, question.question)
    except CodeLensError as exc:  # one failed call must not lose the rest of the run
        logger.error("Question %s failed: %s", question.id, exc.message)
        return AnswerRecord(question.id, question.answerable, "", False, [], False, 0, 0.0,
                            error=exc.message)  # fmt: skip
    seconds = time.perf_counter() - started

    record = AnswerRecord(
        question_id=question.id,
        answerable=question.answerable,
        answer=answer.text,
        refused=NOT_FOUND_ANSWER in answer.text,
        shown=[hit.citation for hit in answer.context],
        citations=[hit.citation for hit in answer.citations],
        correct_citation=any(is_relevant(hit, question.expected) for hit in answer.citations),
        ungrounded=answer.ungrounded_citations,
        seconds=seconds,
    )
    if question.answerable and not record.refused:
        reference = [c for c in chunks if is_relevant(c, question.expected)]
        verdict = judge.judge(question.question, answer.text, answer.context, reference)
        record.verdict = asdict(verdict) if verdict else None
        record.judge_failed = verdict is None
    return record


def summarize_answers(records: list[AnswerRecord]) -> dict[str, float | int]:
    """Count what happened across all answers. Reported as counts, because n is small."""
    answerable = [r for r in records if r.answerable and not r.error]
    unanswerable = [r for r in records if not r.answerable and not r.error]
    answered = [r for r in answerable if not r.refused]
    verdicts = [Verdict(**r.verdict) for r in answered if r.verdict]
    scores = [v.faithfulness for v in verdicts if v.faithfulness is not None]

    return {
        "answerable": len(answerable),
        "false_refusals": len(answerable) - len(answered),
        "with_citation": sum(bool(r.citations) for r in answered),
        "correct_citation": sum(r.correct_citation for r in answered),
        "ungrounded_citations": sum(r.ungrounded for r in answerable),
        "faithfulness": round(statistics.mean(scores), 3) if scores else None,
        "judged": len(verdicts),
        "judge_failed": sum(r.judge_failed for r in answered),
        "correct_yes": sum(v.correct == "yes" for v in verdicts),
        "correct_partial": sum(v.correct == "partial" for v in verdicts),
        "correct_no": sum(v.correct == "no" for v in verdicts),
        "unanswerable": len(unanswerable),
        "correct_refusals": sum(r.refused for r in unanswerable),
        "errors": sum(bool(r.error) for r in records),
        "latency_p50_s": round(statistics.median(r.seconds for r in records if not r.error), 2)
        if any(not r.error for r in records)
        else None,
    }
