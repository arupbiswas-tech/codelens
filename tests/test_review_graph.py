"""Tests for the review graph: routing, the file loop, budgets, and both layers of checking.

The language model and the code search are replaced by fakes, so no network or database is used.
"""

import json
import re

import pytest

from codelens.config import Settings
from codelens.errors import (
    DiffTooLargeError,
    EmbeddingError,
    InvalidInputError,
    LLMError,
    LLMNotConfiguredError,
    NotFoundError,
    ReviewFailedError,
)
from codelens.retrieval import Hit
from codelens.review import graph
from codelens.review.graph import Reviewer, _overlaps_change
from codelens.review.models import ChangedFile


def file_diff(path: str, added=("x = 1",)) -> str:
    """A one-hunk diff that adds `added` lines between two unchanged lines (new lines 2..)."""
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,2 +1,{2 + len(added)} @@\n keep1\n{body} keep2\n"
    )


def review_reply(*comments) -> str:
    return json.dumps({"comments": list(comments)})


def comment(line=2, severity="high", body="This breaks.", evidence=()) -> dict:
    return {"line": line, "severity": severity, "body": body, "evidence": list(evidence)}


class FakeLLM:
    """Plays both roles. `reviews[path]` is a reply, an exception, or a list of those (one
    per attempt). `check` is a reply, an exception, or None for "everything supported"."""

    def __init__(self, reviews=None, check=None):
        self.reviews = reviews or {}
        self.check = check
        self.review_calls: list[tuple[str, str]] = []  # (path, prompt)
        self.check_calls = 0
        self.options: list[dict] = []  # reasoning_effort / max_tokens of every call

    def complete(self, system, user, json_mode=False, **options):
        assert json_mode, "the reviewer must ask for JSON mode"
        self.options.append(options)
        if system.startswith("You verify"):
            self.check_calls += 1
            return self._check_reply(user)
        path = re.search(r"FILE: (\S+)", user)[1]
        self.review_calls.append((path, user))
        reply = self.reviews.get(path, review_reply(comment()))
        if isinstance(reply, list):
            reply = reply[min(len(self.review_calls_for(path)) - 1, len(reply) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def review_calls_for(self, path):
        return [c for c in self.review_calls if c[0] == path]

    def _check_reply(self, user):
        if isinstance(self.check, Exception):
            raise self.check
        if self.check is not None:
            return self.check
        count = len(re.findall(r"^\d+\. ", user, flags=re.MULTILINE))
        verdicts = [{"id": i, "verdict": "supported", "reason": "ok"} for i in range(count)]
        return json.dumps({"verdicts": verdicts})


class FakeEmbedder:
    def __init__(self, fail=False):
        self.fail = fail

    def embed(self, texts):
        if self.fail:
            raise EmbeddingError("down")
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def searches(monkeypatch):
    """Replace the database lookups; `state` lets a test choose what they return."""
    state = {"repo_id": 1, "hits": []}
    monkeypatch.setattr(graph, "find_repo_id", lambda pool, name: state["repo_id"])
    monkeypatch.setattr(graph, "hybrid_search", lambda *args, **kwargs: state["hits"])
    return state


def make_reviewer(llm="default", embedder=None, clock=None, **settings) -> Reviewer:
    kwargs = {"clock": clock} if clock else {}
    return Reviewer(
        pool=None,
        embedder=embedder or FakeEmbedder(),
        llm=FakeLLM() if llm == "default" else llm,
        settings=Settings(database_url="postgresql://unused", **settings),
        **kwargs,
    )


# ---- the happy path and the loop ------------------------------------------------------


def test_each_file_is_reviewed_and_its_comments_are_kept(searches):
    llm = FakeLLM({"a.py": review_reply(comment(2)), "b.py": review_reply(comment(3, "low"))})
    diff = file_diff("a.py") + file_diff("b.py", added=("p = 1", "q = 2"))

    result = make_reviewer(llm).run("o/r", diff)

    assert [(c.path, c.line, c.severity) for c in result.comments] == [
        ("a.py", 2, "high"),
        ("b.py", 3, "low"),
    ]
    assert result.files_reviewed == 2
    assert result.dropped == [] and result.errors == [] and not result.partial
    assert all(c.checked for c in result.comments)
    assert llm.check_calls == 2  # one batched check per file


def test_comments_come_back_most_severe_first(searches):
    llm = FakeLLM(
        {
            "a.py": review_reply(comment(2, "low", "minor"), comment(3, "high", "serious")),
            "b.py": review_reply(comment(2, "medium", "middling")),
        }
    )
    diff = file_diff("a.py", added=("x = 1", "y = 2")) + file_diff("b.py")

    result = make_reviewer(llm).run("o/r", diff)

    assert [c.severity for c in result.comments] == ["high", "medium", "low"]


def test_a_correct_change_can_have_no_comments_and_no_check_call(searches):
    llm = FakeLLM({"a.py": review_reply()})

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert result.comments == [] and result.files_reviewed == 1
    assert llm.check_calls == 0  # nothing to verify


def test_a_diff_with_nothing_reviewable_makes_no_llm_call(searches):
    llm = FakeLLM()
    readme = "diff --git a/R.md b/R.md\n--- a/R.md\n+++ b/R.md\n@@ -1,1 +1,2 @@\n a\n+b\n"

    result = make_reviewer(llm).run("o/r", readme)

    assert result.comments == [] and result.files_reviewed == 0
    assert result.files_skipped == {"not_code": 1}
    assert llm.review_calls == [] and llm.check_calls == 0


# ---- budgets --------------------------------------------------------------------------


def test_the_loop_covers_more_files_than_langgraphs_default_step_limit(searches):
    """LangGraph stops after 25 steps by default; 8 files take 33 steps. The reviewer
    raises the limit, so a full-size review must not fail."""
    diff = "".join(file_diff(f"f{i}.py") for i in range(8))

    result = make_reviewer(review_max_files=8).run("o/r", diff)

    assert result.files_reviewed == 8 and not result.partial


def test_the_file_cap_stops_the_review_and_marks_it_partial(searches):
    llm = FakeLLM()
    diff = "".join(file_diff(f"f{i}.py") for i in range(10))

    result = make_reviewer(llm, review_max_files=3).run("o/r", diff)

    assert result.files_reviewed == 3 and result.partial
    assert len(llm.review_calls) == 3


def test_the_deadline_stops_new_files_but_keeps_what_was_done(searches):
    times = iter([0.0, 1.0, 999.0, 999.0])  # started; first pick (in time); second pick (too late)
    llm = FakeLLM()
    diff = file_diff("a.py") + file_diff("b.py")

    result = make_reviewer(llm, clock=lambda: next(times), review_deadline_seconds=90).run(
        "o/r", diff
    )

    assert result.files_reviewed == 1 and result.partial
    assert len(result.comments) == 1


# ---- layer 1: mechanical checks -------------------------------------------------------


def test_a_comment_on_a_line_that_was_not_added_is_dropped_with_its_reason(searches):
    llm = FakeLLM({"a.py": review_reply(comment(1), comment(2), comment(40))})

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert [c.line for c in result.comments] == [2]
    assert sorted(d.comment.line for d in result.dropped) == [1, 40]
    assert {d.reason for d in result.dropped} == {"not_an_added_line"}


def test_when_every_comment_fails_layer_1_the_llm_check_is_not_called(searches):
    llm = FakeLLM({"a.py": review_reply(comment(77))})

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert result.comments == [] and llm.check_calls == 0


def test_evidence_that_was_never_retrieved_is_removed_from_the_comment(searches):
    searches["hits"] = [Hit(1, "src/db.py", "save", "function", 10, 40, "code")]
    llm = FakeLLM({"a.py": review_reply(comment(2, evidence=["src/db.py:L12-L20", "ghost.py:L1"]))})

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert result.comments[0].evidence == ("src/db.py:L10-L40",)


# ---- layer 2: the LLM check -----------------------------------------------------------


def test_comments_the_checker_does_not_support_are_dropped_with_its_reason(searches):
    verdicts = {
        "verdicts": [
            {"id": 0, "verdict": "supported", "reason": "real"},
            {"id": 1, "verdict": "unsupported", "reason": "the code does not do that"},
        ]
    }
    llm = FakeLLM(
        {"a.py": review_reply(comment(2, body="real bug"), comment(3, body="imagined bug"))},
        check=json.dumps(verdicts),
    )

    result = make_reviewer(llm).run("o/r", file_diff("a.py", added=("x = 1", "y = 2")))

    assert [c.body for c in result.comments] == ["real bug"]
    [dropped] = result.dropped
    assert (dropped.comment.body, dropped.reason) == ("imagined bug", "unsupported")
    assert dropped.detail == "the code does not do that"


def test_a_comment_the_checker_never_mentions_is_not_vouched_for(searches):
    llm = FakeLLM({"a.py": review_reply(comment(2))}, check=json.dumps({"verdicts": []}))

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert result.comments == []
    assert result.dropped[0].reason == "unsupported"


@pytest.mark.parametrize("check", [LLMError("down"), "not json", json.dumps({"x": 1})])
def test_when_the_checker_itself_fails_comments_are_kept_but_marked_unchecked(searches, check):
    llm = FakeLLM({"a.py": review_reply(comment(2))}, check=check)

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert [c.checked for c in result.comments] == [False]
    assert result.dropped == []
    assert any("could not be self-checked" in e for e in result.errors)


# ---- failures of the review call ------------------------------------------------------


def test_a_garbled_reply_is_asked_for_again_once(searches):
    llm = FakeLLM({"a.py": ["sorry, here is prose", review_reply(comment(2))]})

    result = make_reviewer(llm).run("o/r", file_diff("a.py"))

    assert len(result.comments) == 1
    assert len(llm.review_calls) == 2


def test_one_failing_file_is_reported_and_the_others_are_still_reviewed(searches):
    llm = FakeLLM({"a.py": LLMError("The language model is busy; please retry shortly.")})

    result = make_reviewer(llm).run("o/r", file_diff("a.py") + file_diff("b.py"))

    assert [c.path for c in result.comments] == ["b.py"]
    assert any(e.startswith("a.py: review failed") for e in result.errors)
    assert len(llm.review_calls_for("a.py")) == 1  # an API error is not retried in a loop


def test_when_every_file_fails_the_review_fails(searches):
    llm = FakeLLM({"a.py": LLMError("busy"), "b.py": "garbage"})

    with pytest.raises(ReviewFailedError, match="all 2 file"):
        make_reviewer(llm).run("o/r", file_diff("a.py") + file_diff("b.py"))


# ---- related code ---------------------------------------------------------------------


def test_related_code_is_shown_to_the_reviewer_up_to_the_configured_count(searches):
    searches["hits"] = [
        Hit(i, f"lib{i}.py", f"fn{i}", "function", 1, 9, f"body{i}") for i in range(5)
    ]
    llm = FakeLLM()

    make_reviewer(llm, review_context_chunks=2).run("o/r", file_diff("a.py"))

    prompt = llm.review_calls[0][1]
    assert "### [lib0.py:L1-L9] (fn0)" in prompt and "### [lib1.py:L1-L9] (fn1)" in prompt
    assert "lib2.py" not in prompt


def test_the_old_version_of_the_code_being_changed_is_not_offered_as_related_code(searches):
    old_version = Hit(1, "a.py", "f", "function", 1, 2, "the old code")  # overlaps old lines 1-2
    other_part = Hit(2, "a.py", "g", "function", 50, 60, "elsewhere in the same file")
    searches["hits"] = [old_version, other_part]
    llm = FakeLLM()

    make_reviewer(llm).run("o/r", file_diff("a.py"))

    prompt = llm.review_calls[0][1]
    assert "the old code" not in prompt
    assert "elsewhere in the same file" in prompt


def test_overlap_means_same_file_and_intersecting_old_lines():
    changed = ChangedFile("a.py", {}, old_ranges=((10, 14), (40, 42)), rendered="")

    def hit(path, start, end):
        return Hit(1, path, None, "function", start, end, "")

    assert _overlaps_change(hit("a.py", 1, 10), changed)  # touches the first line
    assert _overlaps_change(hit("a.py", 41, 90), changed)  # second hunk
    assert not _overlaps_change(hit("a.py", 15, 39), changed)  # between the hunks
    assert not _overlaps_change(hit("b.py", 10, 14), changed)  # another file


def test_the_review_still_works_when_related_code_cannot_be_embedded(searches):
    llm = FakeLLM()

    result = make_reviewer(llm, embedder=FakeEmbedder(fail=True)).run("o/r", file_diff("a.py"))

    assert len(result.comments) == 1
    assert any("related code unavailable" in e for e in result.errors)
    assert "(none found)" in llm.review_calls[0][1]


def test_a_context_size_of_zero_skips_the_search_entirely(searches, monkeypatch):
    def must_not_run(*_a, **_k):
        raise AssertionError("no search wanted")

    monkeypatch.setattr(graph, "hybrid_search", must_not_run)

    assert make_reviewer(review_context_chunks=0).run("o/r", file_diff("a.py")).files_reviewed == 1


# ---- bad requests ---------------------------------------------------------------------


def test_an_unindexed_repo_is_a_404_before_any_work(searches):
    searches["repo_id"] = None
    llm = FakeLLM()

    with pytest.raises(NotFoundError, match="not indexed"):
        make_reviewer(llm).run("nobody/nothing", file_diff("a.py"))
    assert llm.review_calls == []


def test_a_missing_api_key_fails_before_any_work(searches):
    with pytest.raises(LLMNotConfiguredError):
        make_reviewer(llm=None).run("o/r", file_diff("a.py"))


def test_an_oversized_diff_is_rejected(searches):
    with pytest.raises(DiffTooLargeError):
        make_reviewer(max_diff_bytes=50).run("o/r", file_diff("a.py"))


@pytest.mark.parametrize("text", ["", "not a diff at all"])
def test_text_that_is_not_a_diff_is_a_clean_422_error(searches, text):
    with pytest.raises(InvalidInputError):
        make_reviewer().run("o/r", text)


# ---- reasoning effort and output budget -----------------------------------------------


def test_the_review_pass_thinks_briefly_and_the_check_pass_thinks_harder(searches):
    llm = FakeLLM()

    make_reviewer(llm, review_max_output_tokens=3000).run("o/r", file_diff("a.py"))

    review_options, check_options = llm.options
    assert review_options == {"reasoning_effort": "low", "max_tokens": 3000}
    assert check_options == {"reasoning_effort": "medium", "max_tokens": 3000}


def test_an_empty_effort_setting_means_the_model_default(searches):
    llm = FakeLLM()

    make_reviewer(llm, review_reasoning_effort="", review_check_reasoning_effort=None).run(
        "o/r", file_diff("a.py")
    )

    assert [o["reasoning_effort"] for o in llm.options] == [None, None]


def test_a_comment_whose_main_point_is_right_but_has_a_wrong_side_remark_is_kept_with_a_note(
    searches,
):
    """Found by the evaluation: a real SQL-injection finding was thrown away because of one
    wrong aside. The checker can now say "partly", and the comment is kept with its caveat."""
    verdicts = {
        "verdicts": [
            {"id": 0, "verdict": "partly", "reason": "the fetchall remark is not supported"},
            {"id": 1, "verdict": "supported", "reason": "ok"},
        ]
    }
    llm = FakeLLM(
        {"a.py": review_reply(comment(2, body="injection"), comment(3, body="clean finding"))},
        check=json.dumps(verdicts),
    )

    result = make_reviewer(llm).run("o/r", file_diff("a.py", added=("x = 1", "y = 2")))

    by_body = {c.body: c for c in result.comments}
    assert set(by_body) == {"injection", "clean finding"}
    assert by_body["injection"].note == "the fetchall remark is not supported"
    assert by_body["clean finding"].note == ""
    assert result.dropped == []
