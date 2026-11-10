"""Measure the PR reviewer on seeded-bug diffs:  python -m codelens.evaluation.reviews

`eval/review_cases.json` holds real `git diff`s of one-line mutations: bug cases with a known
location, and clean refactors that should get no comments. For every case we record what the
reviewer WROTE (the draft), what survived the rule check, and what survived the LLM check, all
from one single run. So the effect of each checking layer is measured without extra LLM calls.
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from codelens.config import get_settings
from codelens.db import create_pool
from codelens.embeddings import Embedder
from codelens.errors import CodeLensError
from codelens.evaluation.dataset import DatasetError, Label, load_dataset
from codelens.evaluation.runner import Context, index_dataset_repos
from codelens.logging_setup import setup_logging
from codelens.main import build_llm
from codelens.reranker import Reranker
from codelens.review.graph import Reviewer
from codelens.review.models import ReviewResult

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3  # stop after this many failed cases in a row (quota or outage)
LEVELS = ("draft", "after rule check", "final")


@dataclass(frozen=True)
class ReviewCase:
    id: str
    repo: str
    kind: str  # "bug" or "clean"
    description: str
    diff: str
    expected: tuple[Label, ...]  # where the seeded bug is (empty for clean cases)


@dataclass
class CaseRecord:
    case_id: str
    kind: str
    seconds: float = 0.0
    error: str | None = None
    # Comments at each level, as plain dicts: {"path", "line", "severity", "body", ...}
    levels: dict[str, list[dict]] = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)  # {"reason", "line", "body", "detail"}


def load_review_cases(path: Path) -> list[ReviewCase]:
    """Read and validate the case file, so a typo fails here and not mid-run."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Cannot read {path}: {exc}") from exc

    cases = []
    for item in raw.get("cases", []):
        labels = tuple(Label(e["path"], lines=tuple(e["lines"])) for e in item.get("expected", []))
        if item.get("kind") not in ("bug", "clean"):
            raise DatasetError(f"Case {item.get('id')}: kind must be 'bug' or 'clean'.")
        if (item["kind"] == "bug") != bool(labels):
            raise DatasetError(
                f"Case {item['id']}: bug cases need labels, clean cases must have none."
            )
        cases.append(
            ReviewCase(
                item["id"], item["repo"], item["kind"], item["description"], item["diff"], labels
            )
        )
    if not cases:
        raise DatasetError("The review case file has no cases.")
    ids = [case.id for case in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise DatasetError(f"Duplicate case ids: {duplicates}")
    return cases


def load_previous_records(path: Path | None) -> list[CaseRecord] | None:
    """Read the records of an earlier run, if --resume was given."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [CaseRecord(**record) for record in data["records"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DatasetError(f"cannot read {path}: {exc}") from exc


def levels_of(result: ReviewResult) -> dict[str, list[dict]]:
    """The comments at each stage of checking, rebuilt from one reviewer run.

    final             = comments that were kept
    after rule check  = kept + those only the LLM check rejected (they passed the rules)
    draft             = everything the model wrote
    """
    kept = [asdict(c) for c in result.comments]
    llm_rejected = [asdict(d.comment) for d in result.dropped if d.reason == "unsupported"]
    everything = [asdict(d.comment) for d in result.dropped]
    return {
        "draft": kept + everything,
        "after rule check": kept + llm_rejected,
        "final": kept,
    }


def hits_bug(comments: list[dict], labels: tuple[Label, ...]) -> bool:
    """Is any comment on the seeded line(s)?"""
    return any(
        c["path"] == label.path and label.lines[0] <= c["line"] <= label.lines[1]
        for c in comments
        for label in labels
    )


def run_cases(
    reviewer: Reviewer,
    cases: list[ReviewCase],
    pause: float = 0.0,
    previous: list[CaseRecord] | None = None,
) -> list[CaseRecord]:
    """Review every case. Stops early after repeated failures (almost always the LLM quota).

    `previous` are records of an interrupted run: cases that completed there are kept as
    they are, so only the failed or missing ones cost LLM calls.
    """
    already_done = {r.case_id: r for r in previous or [] if r.levels}
    records: list[CaseRecord] = []
    errors_in_a_row = 0
    for index, case in enumerate(cases, start=1):
        if case.id in already_done:
            records.append(already_done[case.id])
            continue
        logger.info("Reviewing case %d/%d: %s", index, len(cases), case.id)
        record = CaseRecord(case.id, case.kind)
        started = time.perf_counter()
        try:
            result = reviewer.run(case.repo, case.diff)
            record.levels = levels_of(result)
            record.dropped = [
                {
                    "reason": d.reason,
                    "line": d.comment.line,
                    "body": d.comment.body,
                    "detail": d.detail,
                }
                for d in result.dropped
            ]
            record.error = "; ".join(result.errors) or None
        except CodeLensError as exc:
            logger.error("Case %s failed: %s", case.id, exc.message)
            record.error = exc.message
            record.levels = {}
        record.seconds = round(time.perf_counter() - started, 2)
        records.append(record)

        errors_in_a_row = errors_in_a_row + 1 if not record.levels else 0
        if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
            logger.error("Stopping early after %d failures in a row.", errors_in_a_row)
            break
        time.sleep(pause)
    return records


def summarize(records: list[CaseRecord], cases: list[ReviewCase]) -> dict:
    """Counts per level: seeded bugs found, comments on clean diffs, other comments on bug diffs."""
    by_id = {case.id: case for case in cases}
    done = [r for r in records if r.levels]  # cases that ran
    bugs = [r for r in done if r.kind == "bug"]
    cleans = [r for r in done if r.kind == "clean"]

    table = {}
    for level in LEVELS:
        found = sum(hits_bug(r.levels[level], by_id[r.case_id].expected) for r in bugs)
        on_clean = sum(len(r.levels[level]) for r in cleans)
        elsewhere = sum(
            len([c for c in r.levels[level] if not hits_bug([c], by_id[r.case_id].expected)])
            for r in bugs
        )
        table[level] = {
            "bugs_found": found,
            "on_clean_diffs": on_clean,
            "elsewhere_on_bug_diffs": elsewhere,
        }

    reasons: dict[str, int] = {}
    for record in done:
        for item in record.dropped:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {
        "bug_cases": len(bugs),
        "clean_cases": len(cleans),
        "failed_cases": len(cases) - len(done),  # failed, or never run after an early stop
        "levels": table,
        "dropped_by_reason": reasons,
        "unchecked_comments": sum(
            not c.get("checked", True) for r in done for c in r.levels["final"]
        ),
    }


def table_markdown(summary: dict) -> str:
    """The results table, with counts."""
    bugs, cleans = summary["bug_cases"], summary["clean_cases"]
    lines = [
        f"| Stage | Seeded bugs found (of {bugs}) | Comments on clean diffs ({cleans} diffs) "
        "| Other comments on bug diffs |",
        "|---|---|---|---|",
    ]
    for level in LEVELS:
        row = summary["levels"][level]
        cells = [row["bugs_found"], row["on_clean_diffs"], row["elsewhere_on_bug_diffs"]]
        lines.append(f"| {level} | " + " | ".join(str(cell) for cell in cells) + " |")
    note = f"\nDropped by the checks: {summary['dropped_by_reason'] or 'nothing'}."
    if summary["unchecked_comments"]:
        note += (
            f"\n**{summary['unchecked_comments']} final comment(s) could not be self-checked** "
            "(the checker call failed), so the 'final' row includes unverified comments."
        )
    if summary["failed_cases"]:
        note += (
            f"\n**INCOMPLETE: {summary['failed_cases']} case(s) failed or were never run, "
            "and are not counted.**"
        )
    return "\n".join(lines) + note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the PR reviewer on seeded-bug diffs.")
    parser.add_argument("--cases", type=Path, default=Path("eval/review_cases.json"))
    parser.add_argument("--dataset", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--out", type=Path, default=Path("eval/results"))
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between cases")
    parser.add_argument(
        "--resume",
        type=Path,
        help="results JSON of an interrupted run: keep its finished cases, redo the rest",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        cases = load_review_cases(args.cases)
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    try:
        previous = load_previous_records(args.resume)
    except DatasetError as exc:
        print(f"resume error: {exc}", file=sys.stderr)
        return 2
    llm = build_llm(settings)
    if llm is None:
        print("GROQ_API_KEY is not set.", file=sys.stderr)
        return 2

    pool = create_pool(settings)
    try:
        embedder = Embedder(
            settings.embedding_model, settings.embedding_dim, settings.embedding_batch_size
        )
        ctx = Context(pool, embedder, Reranker(settings.reranker_model), settings, llm=llm)
        print("Making sure the repos are indexed (unchanged ones are reused)...")
        index_dataset_repos(ctx, dataset)

        records = run_cases(Reviewer(pool, embedder, llm, settings), cases, args.pause, previous)
        summary = summarize(records, cases)
    except CodeLensError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        pool.close()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    results = {"summary": summary, "records": [asdict(r) for r in records]}
    (args.out / f"review-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (args.out / "latest-review.md").write_text(table_markdown(summary) + "\n", encoding="utf-8")
    print("\n" + table_markdown(summary))
    print(f"\nSaved {args.out / f'review-{stamp}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
