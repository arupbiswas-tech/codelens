"""The PR reviewer as a LangGraph: one pass of the graph reviews one file, and it loops.

    parse_diff --(nothing reviewable)--> END
        |
        v
    pick_file --(no files left, file cap or deadline reached)--> END
        |   ^
        v   |
    retrieve_context -> review -> self_check

The loop is what makes this a graph and not a straight line: the number of passes depends
on the diff, and `pick_file` is the single place that enforces the budget.
"""

import logging
import operator
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from psycopg_pool import ConnectionPool

from codelens.config import Settings
from codelens.embeddings import Embedder
from codelens.errors import (
    DiffTooLargeError,
    EmbeddingError,
    LLMError,
    LLMNotConfiguredError,
    NotFoundError,
    ReviewFailedError,
)
from codelens.ingestion.models import text_for_model
from codelens.llm import LLMClient
from codelens.retrieval import Hit, find_repo_id, hybrid_search
from codelens.review.checks import (
    SEVERITY_ORDER,
    anchor_and_dedupe,
    parse_comments,
    parse_verdicts,
    verify_evidence,
)
from codelens.review.diff import parse_diff
from codelens.review.models import ChangedFile, Comment, DroppedComment, ReviewResult
from codelens.review.prompts import (
    CHECK_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    build_check_prompt,
    build_review_prompt,
)

logger = logging.getLogger(__name__)

STEPS_PER_FILE = 4  # pick_file, retrieve_context, review, self_check
QUERY_CHARS = 1500  # how much of a file's added code is used to search for related code


class ReviewState(TypedDict, total=False):
    """Everything that flows through the graph.

    Fields with `operator.add` accumulate: each node returns only what it adds.
    """

    diff: str
    repo_id: int
    started_at: float
    files: list[ChangedFile]
    skipped: dict[str, int]
    next_index: int  # the next file to pick
    reviewed: int  # files picked so far (each one costs LLM calls)
    partial: bool  # a limit stopped the review early
    current: ChangedFile | None  # the file of this pass
    context: list[Hit]  # related code for the current file
    drafts: list[Comment]  # what the model wrote for the current file
    comments: Annotated[list[Comment], operator.add]  # kept comments, over all files
    dropped: Annotated[list[DroppedComment], operator.add]
    errors: Annotated[list[str], operator.add]
    review_failures: Annotated[int, operator.add]  # files whose review call failed


class Reviewer:
    """Builds the graph once and runs it for every request."""

    def __init__(
        self,
        pool: ConnectionPool,
        embedder: Embedder,
        llm: LLMClient | None,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,  # replaced in tests
    ):
        self._pool = pool
        self._embedder = embedder
        self._llm = llm
        self._settings = settings
        self._clock = clock
        self._graph = self._build_graph()

    # ---- public entry point -----------------------------------------------------------

    def run(self, repo_name: str, diff: str) -> ReviewResult:
        """Review `diff` against the indexed repo. Raises a CodeLensError for bad input."""
        if len(diff.encode("utf-8")) > self._settings.max_diff_bytes:
            raise DiffTooLargeError(
                f"The diff is larger than {self._settings.max_diff_bytes // 1000} KB."
            )
        if self._llm is None:  # fail before doing any work we would have to throw away
            raise LLMNotConfiguredError("Set GROQ_API_KEY to enable pull request reviews.")
        repo_id = find_repo_id(self._pool, repo_name.strip())
        if repo_id is None:
            raise NotFoundError(f"'{repo_name}' is not indexed. Index it first with POST /repos.")

        initial: ReviewState = {
            "diff": diff,
            "repo_id": repo_id,
            "started_at": self._clock(),
            "comments": [],
            "dropped": [],
            "errors": [],
            "review_failures": 0,
        }
        # LangGraph stops a run after `recursion_limit` steps (default 25). Our loop takes
        # four steps per file, so the default would break any diff of more than five files.
        limit = STEPS_PER_FILE * self._settings.review_max_files + 10
        final = self._graph.invoke(initial, config={"recursion_limit": limit})
        return self._to_result(final)

    # ---- graph wiring -----------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(ReviewState)
        graph.add_node("parse_diff", self._parse_diff)
        graph.add_node("pick_file", self._pick_file)
        graph.add_node("retrieve_context", self._retrieve_context)
        graph.add_node("review", self._review)
        graph.add_node("self_check", self._self_check)

        graph.add_edge(START, "parse_diff")
        graph.add_conditional_edges(
            "parse_diff", self._after_parse, {"pick_file": "pick_file", END: END}
        )
        graph.add_conditional_edges(
            "pick_file", self._after_pick, {"retrieve_context": "retrieve_context", END: END}
        )
        graph.add_edge("retrieve_context", "review")
        graph.add_edge("review", "self_check")
        graph.add_edge("self_check", "pick_file")  # the loop
        return graph.compile()

    @staticmethod
    def _after_parse(state: ReviewState) -> str:
        return "pick_file" if state["files"] else END

    @staticmethod
    def _after_pick(state: ReviewState) -> str:
        return "retrieve_context" if state["current"] is not None else END

    # ---- nodes ------------------------------------------------------------------------

    def _parse_diff(self, state: ReviewState) -> dict:
        files, skipped = parse_diff(state["diff"], self._settings.review_max_added_lines)
        return {"files": files, "skipped": dict(skipped), "next_index": 0, "reviewed": 0}

    def _pick_file(self, state: ReviewState) -> dict:
        """Choose the next file, or stop because there is none or a budget is used up."""
        index = state["next_index"]
        if index >= len(state["files"]):
            return {"current": None}

        out_of_files = state["reviewed"] >= self._settings.review_max_files
        out_of_time = self._clock() - state["started_at"] > self._settings.review_deadline_seconds
        if out_of_files or out_of_time:
            return {"current": None, "partial": True}

        return {
            "current": state["files"][index],
            "next_index": index + 1,
            "reviewed": state["reviewed"] + 1,
        }

    def _retrieve_context(self, state: ReviewState) -> dict:
        """Find related code in the repo for the file being reviewed."""
        file, settings = state["current"], self._settings
        if settings.review_context_chunks == 0:
            return {"context": []}

        query = "\n".join(file.added.values())[:QUERY_CHARS]
        try:
            vector = self._embedder.embed([text_for_model(file.path, None, query)])[0]
        except EmbeddingError as exc:  # no context is worse than context, but not fatal
            logger.warning("No related code for %s: %s", file.path, exc.message)
            return {"context": [], "errors": [f"{file.path}: related code unavailable"]}

        hits = hybrid_search(
            self._pool,
            state["repo_id"],
            query,
            vector,
            per_search=settings.search_candidates,
            limit=settings.fused_candidates,
        )
        related = [hit for hit in hits if not _overlaps_change(hit, file)]
        return {"context": related[: settings.review_context_chunks]}

    def _review(self, state: ReviewState) -> dict:
        """Ask the model for comments on the current file (one retry if the reply is garbled)."""
        file = state["current"]
        system = REVIEW_SYSTEM_PROMPT.format(
            max_comments=self._settings.review_max_comments_per_file
        )
        prompt = build_review_prompt(file, state["context"])

        problem = "the reply was not valid review JSON"
        for _attempt in range(2):
            try:
                reply = self._llm.complete(
                    system, prompt, json_mode=True, **self._llm_options("review")
                )
            except LLMError as exc:
                problem = exc.message
                break  # the client already retried transient errors; do not hammer a failing API
            comments = parse_comments(reply, file.path, self._settings.review_max_comments_per_file)
            if comments is not None:
                return {"drafts": comments}

        logger.error("Review of %s failed: %s", file.path, problem)
        return {
            "drafts": [],
            "errors": [f"{file.path}: review failed ({problem})"],
            "review_failures": 1,
        }

    def _self_check(self, state: ReviewState) -> dict:
        """Drop comments that are not grounded: first by rule, then with a second LLM look."""
        file, context = state["current"], state["context"]

        # Layer 1 (free and certain): the line must be an added line; evidence must be real.
        anchored, dropped = anchor_and_dedupe(state["drafts"], file)
        anchored = [verify_evidence(comment, context) for comment in anchored]
        if not anchored:
            return {"dropped": dropped}

        # Layer 2 (one LLM call for all comments of the file): is each one actually supported?
        kept, rejected, error = self._llm_check(file, anchored, context)
        update: dict = {"comments": kept, "dropped": dropped + rejected}
        if error:
            update["errors"] = [error]
        return update

    def _llm_check(
        self, file: ChangedFile, comments: list[Comment], context: list[Hit]
    ) -> tuple[list[Comment], list[DroppedComment], str | None]:
        """Return (kept, rejected, problem). If the check itself fails, nothing is rejected."""
        prompt = build_check_prompt(file, comments, context)
        try:
            reply = self._llm.complete(
                CHECK_SYSTEM_PROMPT, prompt, json_mode=True, **self._llm_options("check")
            )
            verdicts = parse_verdicts(reply, len(comments))
        except LLMError:
            verdicts = None
        if verdicts is None:
            # Fail open, visibly: a broken checker must not silently delete a whole review,
            # so the comments are returned marked as unchecked.
            unchecked = [replace(comment, checked=False) for comment in comments]
            return unchecked, [], f"{file.path}: comments could not be self-checked"

        kept, rejected = [], []
        for index, comment in enumerate(comments):
            # A comment the checker never mentioned has not been vouched for: fail closed.
            verdict, reason = verdicts.get(index, ("unsupported", "the checker gave no verdict"))
            if verdict == "supported":
                kept.append(comment)
            elif verdict == "partly":
                # The main finding is real: keep it, with the checker's caveat attached.
                kept.append(replace(comment, note=reason))
            else:
                rejected.append(DroppedComment(comment, "unsupported", reason))
        return kept, rejected, None

    def _llm_options(self, step: str) -> dict:
        """Reasoning effort and output cap for the "review" or "check" call."""
        settings = self._settings
        if step == "review":
            effort = settings.review_reasoning_effort
        else:
            effort = settings.review_check_reasoning_effort
        return {
            "reasoning_effort": effort or None,  # an empty setting means "model default"
            "max_tokens": settings.review_max_output_tokens,
        }

    # ---- result -----------------------------------------------------------------------

    @staticmethod
    def _to_result(state: ReviewState) -> ReviewResult:
        reviewed = state.get("reviewed", 0)
        failures = state.get("review_failures", 0)
        if reviewed > 0 and failures == reviewed:
            raise ReviewFailedError(
                f"The review failed for all {reviewed} file(s): {state['errors'][0]}"
            )
        ordered = sorted(
            state["comments"], key=lambda c: (SEVERITY_ORDER[c.severity], c.path, c.line)
        )
        return ReviewResult(
            comments=ordered,
            dropped=state["dropped"],
            files_reviewed=reviewed,
            files_skipped=state.get("skipped", {}),
            errors=state["errors"],
            partial=state.get("partial", False),
        )


def _overlaps_change(hit: Hit, file: ChangedFile) -> bool:
    """Is this stored chunk the OLD version of code the diff changes?

    The index holds the repo as it was before the pull request, so searching with the
    changed code would mostly return that old code, which is not "related" code but the
    very thing being reviewed. Such chunks are removed from the context.
    """
    return hit.path == file.path and any(
        hit.start_line <= last and hit.end_line >= first for first, last in file.old_ranges
    )
