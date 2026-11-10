"""Evaluation tests: dataset validation, metrics, judge parsing and report formatting."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from codelens.answering import Answer
from codelens.errors import LLMError
from codelens.evaluation import runner
from codelens.evaluation.__main__ import load_previous_answers
from codelens.evaluation.dataset import DatasetError, Label, load_dataset
from codelens.evaluation.judge import Judge, build_judge_prompt, parse_verdict
from codelens.evaluation.metrics import (
    first_relevant_rank,
    is_relevant,
    label_matches,
    summarize_ranks,
)
from codelens.evaluation.report import answers_table, incomplete_warning, misses, retrieval_table
from codelens.evaluation.reviews import (
    LEVELS,
    CaseRecord,
    ReviewCase,
    hits_bug,
    levels_of,
    load_review_cases,
    run_cases,
    summarize,
    table_markdown,
)
from codelens.evaluation.runner import AnswerRecord, check_labels, summarize_answers
from codelens.retrieval import Hit
from codelens.review.models import Comment, DroppedComment, ReviewResult

DATASET_FILE = Path(__file__).resolve().parent.parent / "eval" / "questions.json"


def hit(chunk_id=1, path="a.py", symbol="f", start=10, end=20) -> Hit:
    return Hit(chunk_id, path, symbol, "function", start, end, "code")


# ---- the real dataset file ------------------------------------------------------------


def test_the_shipped_dataset_is_valid_and_big_enough():
    dataset = load_dataset(DATASET_FILE)

    assert 25 <= len(dataset.questions) <= 30
    assert len(dataset.answerable()) >= 20
    assert any(not q.answerable for q in dataset.questions), "needs unanswerable questions too"
    assert all(len(info.commit) == 40 for info in dataset.repos.values())


# ---- dataset validation ---------------------------------------------------------------


def write_dataset(tmp_path, questions, repos=None) -> Path:
    repos = (
        repos if repos is not None else {"o/r": {"url": "https://github.com/o/r", "commit": "c"}}
    )
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"repos": repos, "questions": questions}))
    return path


def question(**overrides) -> dict:
    base = {
        "id": "q1",
        "repo": "o/r",
        "category": "how",
        "question": "Where?",
        "expected": [{"path": "a.py", "symbol": "f"}],
    }
    return {**base, **overrides}


def test_a_valid_dataset_loads_labels_of_both_kinds(tmp_path):
    expected = [{"path": "a.py", "symbol": "f"}, {"path": "b.js", "lines": [3, 9]}]

    dataset = load_dataset(write_dataset(tmp_path, [question(expected=expected)]))

    assert dataset.questions[0].expected == (
        Label("a.py", symbol="f"),
        Label("b.js", lines=(3, 9)),
    )


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (question(repo="x/unknown"), "not listed"),
        (question(expected=[]), "at least one label"),
        (question(answerable=False), "must have no labels"),
        (question(expected=[{"path": "a.py"}]), "exactly one of symbol/lines"),
        (question(expected=[{"path": "a.py", "symbol": "f", "lines": [1, 2]}]), "exactly one"),
        (question(expected=[{"path": "a.py", "lines": [9, 3]}]), "bad line range"),
        (question(expected=[{"symbol": "f"}]), "no 'path'"),
        (question(question=""), "missing 'question'"),
    ],
)
def test_malformed_questions_fail_with_a_clear_message(tmp_path, bad, message):
    with pytest.raises(DatasetError, match=message):
        load_dataset(write_dataset(tmp_path, [bad]))


def test_duplicate_ids_and_unreadable_files_are_rejected(tmp_path):
    with pytest.raises(DatasetError, match="Duplicate"):
        load_dataset(write_dataset(tmp_path, [question(), question()]))
    with pytest.raises(DatasetError, match="Cannot read"):
        load_dataset(tmp_path / "missing.json")


# ---- metrics --------------------------------------------------------------------------


def test_a_symbol_label_matches_path_and_symbol_only():
    label = Label("a.py", symbol="f")

    assert label_matches(hit(), label)
    assert not label_matches(hit(symbol="g"), label)
    assert not label_matches(hit(path="b.py"), label)


def test_a_line_label_matches_any_overlapping_chunk():
    label = Label("a.py", lines=(15, 25))

    assert label_matches(hit(start=10, end=20), label)  # overlaps the start
    assert label_matches(hit(start=20, end=40), label)  # touches one line
    assert not label_matches(hit(start=26, end=30), label)  # just after
    assert not label_matches(hit(start=1, end=14), label)  # just before


def test_any_of_several_alternative_labels_is_enough():
    labels = [Label("a.py", symbol="x"), Label("a.py", symbol="f")]

    assert is_relevant(hit(symbol="f"), labels)
    assert not is_relevant(hit(symbol="z"), labels)


def test_first_relevant_rank_is_one_based_and_none_when_absent():
    hits = [hit(1, symbol="a"), hit(2, symbol="b"), hit(3, symbol="c")]

    assert first_relevant_rank(hits, [Label("a.py", symbol="b")]) == 2
    assert first_relevant_rank(hits, [Label("a.py", symbol="nope")]) is None
    assert first_relevant_rank([], [Label("a.py", symbol="a")]) is None


def test_summary_counts_hits_at_k_and_mean_reciprocal_rank():
    ranks = [1, 2, 5, None]

    summary = summarize_ranks(ranks, ks=(1, 3, 6))

    assert summary["hit@1"] == 0.25
    assert summary["hit@3"] == 0.5
    assert summary["hit@6"] == 0.75
    assert summary["mrr"] == pytest.approx((1 + 1 / 2 + 1 / 5 + 0) / 4)


def test_summary_of_nothing_is_empty():
    assert summarize_ranks([], ks=(1,)) == {}


def test_check_labels_reports_labels_that_match_no_chunk(tmp_path):
    dataset = load_dataset(
        write_dataset(
            tmp_path,
            [question(id="ok"), question(id="typo", expected=[{"path": "a.py", "symbol": "fx"}])],
        )
    )

    problems = check_labels(dataset, {"o/r": [hit(symbol="f")]})

    assert len(problems) == 1 and problems[0].startswith("typo:")


# ---- judge ----------------------------------------------------------------------------


GOOD_REPLY = json.dumps(
    {
        "claims": [{"claim": "a", "supported": True}, {"claim": "b", "supported": False}],
        "correct": "Partial",
    }
)


def test_a_well_formed_verdict_is_parsed():
    verdict = parse_verdict(GOOD_REPLY)

    assert (verdict.claims_total, verdict.claims_supported, verdict.correct) == (2, 1, "partial")
    assert verdict.faithfulness == 0.5


def test_json_surrounded_by_chatter_or_code_fences_is_still_found():
    assert parse_verdict(f"Here you go:\n```json\n{GOOD_REPLY}\n```\nDone.") is not None


@pytest.mark.parametrize(
    "reply",
    [
        "no json at all",
        "{not valid json}",
        '{"claims": [], "correct": "maybe"}',  # unknown verdict word
        '{"correct": "yes"}',  # no claims field
        '{"claims": [{"claim": "x"}], "correct": "yes"}',  # claim without 'supported'
        '{"claims": 5, "correct": "yes"}',
    ],
)
def test_unusable_verdicts_are_rejected_instead_of_guessed(reply):
    assert parse_verdict(reply) is None


def test_an_answer_without_claims_has_no_faithfulness_score():
    assert parse_verdict('{"claims": [], "correct": "no"}').faithfulness is None


def test_the_judge_prompt_contains_all_four_parts():
    prompt = build_judge_prompt("Why?", "Because.", [hit(1)], [hit(2, path="ref.py")])

    assert "QUESTION:\nWhy?" in prompt
    assert "SHOWN SNIPPETS:\n[a.py:L10-L20]" in prompt
    assert "REFERENCE CODE:\n[ref.py:L10-L20]" in prompt
    assert prompt.endswith("ANSWER TO REVIEW:\nBecause.")


class FakeLLM:
    def __init__(self, reply=GOOD_REPLY, error=None):
        self.reply, self.error = reply, error

    def complete(self, system, user):
        if self.error:
            raise self.error
        return self.reply


def test_judge_returns_none_when_the_llm_fails_or_replies_nonsense():
    assert Judge(FakeLLM(error=LLMError("down"))).judge("q", "a", [], []) is None
    assert Judge(FakeLLM(reply="I refuse")).judge("q", "a", [], []) is None


def test_judge_returns_a_verdict_for_a_good_reply():
    assert Judge(FakeLLM()).judge("q", "a", [hit()], [hit()]).claims_total == 2


# ---- answer summary and report --------------------------------------------------------


def record(qid, answerable=True, refused=False, citations=1, correct=True, ungrounded=0, **kw):
    return AnswerRecord(
        question_id=qid,
        answerable=answerable,
        answer="x",
        refused=refused,
        citations=["a.py:L1-L2"] * citations,
        correct_citation=correct,
        ungrounded=ungrounded,
        seconds=kw.pop("seconds", 2.0),
        **kw,
    )


def test_answer_summary_counts_each_outcome():
    records = [
        record("a", verdict={"claims_total": 4, "claims_supported": 4, "correct": "yes"}),
        record("b", citations=0, correct=False, ungrounded=2,
               verdict={"claims_total": 2, "claims_supported": 1, "correct": "no"}),
        record("c", refused=True, citations=0, correct=False),  # wrongly refused
        record("d", answerable=False, refused=True, citations=0, correct=False),  # good refusal
        record("e", answerable=False, citations=0, correct=False),  # hallucinated an answer
        record("f", error="LLM down"),
    ]  # fmt: skip

    summary = summarize_answers(records)

    assert summary["answerable"] == 3  # the errored question is not counted
    assert summary["false_refusals"] == 1
    assert summary["with_citation"] == 1
    assert summary["correct_citation"] == 1
    assert summary["ungrounded_citations"] == 2
    assert summary["faithfulness"] == 0.75  # mean of 1.0 and 0.5
    assert (summary["correct_yes"], summary["correct_partial"], summary["correct_no"]) == (1, 0, 1)
    assert (summary["unanswerable"], summary["correct_refusals"]) == (2, 1)
    assert summary["errors"] == 1


def test_a_judge_failure_is_counted_not_hidden():
    summary = summarize_answers([record("a", judge_failed=True)])

    assert summary["judge_failed"] == 1
    assert summary["judged"] == 0
    assert summary["faithfulness"] is None


def test_retrieval_table_shows_counts_not_percentages():
    stage = {"hit@1": 0.5, "hit@3": 0.75, "hit@6": 1.0, "hit@10": 1.0, "mrr": 0.7}
    summary = {s: stage for s in ("vector", "keyword", "hybrid", "hybrid+rerank")}
    summary["pool"] = {"hit@20": 1.0}

    table = retrieval_table(summary, n=4, pool_size=20)

    assert "| vector search only | 2/4 | 3/4 | 4/4 | 4/4 | 0.70 |" in table
    assert "answer for 4/4 questions" in table


def test_answers_table_lists_the_key_measures():
    summary = summarize_answers(
        [record("a", verdict={"claims_total": 1, "claims_supported": 1, "correct": "yes"})]
    )

    table = answers_table(summary)

    assert "| Answers with at least one verified citation | 1/1 |" in table
    assert "| Faithfulness" in table


def test_misses_lists_only_questions_not_in_the_top_three(tmp_path):
    dataset = load_dataset(
        write_dataset(tmp_path, [question(id="good"), question(id="late"), question(id="lost")])
    )

    text = misses(dataset, {"good": 2, "late": 5, "lost": None})

    assert "good" not in text
    assert "`late`" in text and "rank 5" in text
    assert "`lost`" in text and "not found" in text


# ---- early stop and resume ------------------------------------------------------------


class ScriptedAnswerer:
    """Replaces the real Answerer: fails for the listed question ids, answers the rest."""

    asked: list[str] = []
    failing: set[str] = set()

    def __init__(self, *_args):
        pass

    def ask(self, repo, question):
        ScriptedAnswerer.asked.append(question)
        if question in ScriptedAnswerer.failing:
            raise LLMError("The language model is busy; please retry shortly.")
        return Answer(text="ok [a.py:L10-L20]", citations=[hit()], ungrounded_citations=0)


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(runner, "Answerer", ScriptedAnswerer)
    monkeypatch.setattr(runner, "Judge", lambda llm: Judge(FakeLLM()))
    ScriptedAnswerer.asked, ScriptedAnswerer.failing = [], set()
    return ScriptedAnswerer


def five_question_dataset(tmp_path):
    questions = [question(id=f"q{i}", question=f"Q{i}") for i in range(1, 6)]
    return load_dataset(write_dataset(tmp_path, questions))


def fake_context():
    llm = object()
    return runner.Context(None, None, object(), None, llm=llm, judge_llm=None)


CHUNKS = {"o/r": [hit()]}


def test_the_run_stops_after_three_failures_in_a_row(tmp_path, scripted):
    scripted.failing = {"Q2", "Q3", "Q4", "Q5"}

    records = runner.run_answers(fake_context(), five_question_dataset(tmp_path), CHUNKS)

    assert [r.question_id for r in records] == ["q1", "q2", "q3", "q4"]  # q5 was never asked
    assert scripted.asked == ["Q1", "Q2", "Q3", "Q4"]
    assert [bool(r.error) for r in records] == [False, True, True, True]


def test_a_success_resets_the_failure_streak(tmp_path, scripted):
    scripted.failing = {"Q1", "Q2", "Q4", "Q5"}  # fail, fail, ok, fail, fail: never 3 in a row

    records = runner.run_answers(fake_context(), five_question_dataset(tmp_path), CHUNKS)

    assert len(records) == 5


def test_resuming_keeps_good_answers_and_only_asks_the_rest(tmp_path, scripted):
    dataset = five_question_dataset(tmp_path)
    scripted.failing = {"Q3", "Q4", "Q5"}
    first_run = runner.run_answers(fake_context(), dataset, CHUNKS)
    scripted.asked, scripted.failing = [], set()  # the quota came back

    second_run = runner.run_answers(fake_context(), dataset, CHUNKS, previous=first_run)

    assert scripted.asked == ["Q3", "Q4", "Q5"]  # Q1 and Q2 were not asked again
    assert [r.question_id for r in second_run] == ["q1", "q2", "q3", "q4", "q5"]
    assert not any(r.error for r in second_run)


def test_records_remember_which_chunks_the_model_was_shown(tmp_path, scripted):
    records = runner.run_answers(fake_context(), five_question_dataset(tmp_path), CHUNKS)

    assert records[0].shown == []  # the fake answer carries no context; the field exists


def test_resume_refuses_results_made_with_a_different_reranker_setting(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"config": {"reranker_used_for_answers": False}}))

    with pytest.raises(DatasetError, match="without the reranker"):
        load_previous_answers(path, rerank=True)
    assert load_previous_answers(path, rerank=False) == []
    assert load_previous_answers(None, rerank=True) is None


def test_the_report_warns_loudly_only_when_a_run_is_incomplete():
    complete = {"errors": 0}
    partial = {"errors": 3}

    assert incomplete_warning(complete, recorded=29, total=29) == ""
    assert "INCOMPLETE" in incomplete_warning(partial, recorded=26, total=29)
    assert "3 were never asked" in incomplete_warning({"errors": 0}, recorded=26, total=29)


def test_an_unreadable_resume_file_is_a_clean_error_not_a_traceback(tmp_path):
    with pytest.raises(DatasetError, match="cannot read"):
        load_previous_answers(tmp_path / "missing.json", rerank=True)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(DatasetError, match="cannot read"):
        load_previous_answers(broken, rerank=True)


def test_each_kind_of_run_writes_its_own_latest_file():
    from argparse import Namespace

    from codelens.evaluation.__main__ import latest_name

    assert latest_name(Namespace(no_answers=True, no_rerank=False)) == "latest-retrieval.md"
    assert latest_name(Namespace(no_answers=False, no_rerank=True)) == "latest-no-rerank.md"
    assert latest_name(Namespace(no_answers=False, no_rerank=False)) == "latest.md"


# ---- reviewer evaluation --------------------------------------------------------------


CASES_FILE = Path(__file__).resolve().parent.parent / "eval" / "review_cases.json"


def test_the_shipped_review_cases_are_valid_real_diffs():
    from codelens.review.diff import parse_diff

    cases = load_review_cases(CASES_FILE)
    dataset = load_dataset(DATASET_FILE)

    assert 6 <= len(cases) <= 12
    assert {c.kind for c in cases} == {"bug", "clean"}
    for case in cases:
        assert case.repo in dataset.repos
        files, _ = parse_diff(case.diff, max_added_lines=300)
        assert files, f"{case.id}: the diff has nothing reviewable"
        for label in case.expected:  # the seeded line really is an added line of that file
            [changed] = [f for f in files if f.path == label.path]
            assert all(line in changed.added for line in range(label.lines[0], label.lines[1] + 1))


def write_cases(tmp_path, cases) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": cases}))
    return path


def case_dict(**overrides) -> dict:
    base = {
        "id": "c1",
        "repo": "o/r",
        "kind": "bug",
        "description": "d",
        "diff": "x",
        "expected": [{"path": "a.py", "lines": [5, 5]}],
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (case_dict(kind="weird"), "kind must be"),
        (case_dict(expected=[]), "bug cases need labels"),
        (case_dict(kind="clean"), "clean cases must have none"),
    ],
)
def test_malformed_review_cases_fail_clearly(tmp_path, bad, message):
    with pytest.raises(DatasetError, match=message):
        load_review_cases(write_cases(tmp_path, [bad]))


def c(path="a.py", line=5, body="b") -> Comment:
    return Comment(path, line, "high", body)


def test_levels_are_rebuilt_from_one_run():
    result = ReviewResult(
        comments=[c(line=5)],
        dropped=[
            DroppedComment(c(line=99), "not_an_added_line"),
            DroppedComment(c(line=7, body="imagined"), "unsupported"),
        ],
    )

    levels = levels_of(result)

    assert sorted(x["line"] for x in levels["draft"]) == [5, 7, 99]
    assert sorted(x["line"] for x in levels["after rule check"]) == [5, 7]
    assert [x["line"] for x in levels["final"]] == [5]


def test_a_bug_is_found_only_by_a_comment_on_the_seeded_line():
    labels = (Label("a.py", lines=(5, 6)),)

    assert hits_bug([{"path": "a.py", "line": 6}], labels)
    assert not hits_bug([{"path": "a.py", "line": 7}], labels)
    assert not hits_bug([{"path": "b.py", "line": 5}], labels)
    assert not hits_bug([], labels)


BUG = ReviewCase("bug1", "o/r", "bug", "d", "diff", (Label("a.py", lines=(5, 5)),))
CLEAN = ReviewCase("clean1", "o/r", "clean", "d", "diff", ())


class FakeReviewer:
    def __init__(self, results):
        self.results = results  # case diff -> ReviewResult or exception
        self.calls = 0

    def run(self, repo, diff):
        self.calls += 1
        result = self.results[self.calls - 1]
        if isinstance(result, Exception):
            raise result
        return result


def test_the_summary_shows_what_each_check_removed():
    bug_result = ReviewResult(
        comments=[c(line=5)],
        dropped=[
            DroppedComment(c(line=99), "not_an_added_line"),
            DroppedComment(c(line=7, body="imagined"), "unsupported"),
        ],
    )
    clean_result = ReviewResult(
        comments=[],
        dropped=[DroppedComment(c(line=3, body="nit"), "unsupported")],
    )

    records = run_cases(FakeReviewer([bug_result, clean_result]), [BUG, CLEAN])
    summary = summarize(records, [BUG, CLEAN])

    assert summary["levels"]["draft"] == {
        "bugs_found": 1,
        "on_clean_diffs": 1,
        "elsewhere_on_bug_diffs": 2,
    }
    assert summary["levels"]["after rule check"]["elsewhere_on_bug_diffs"] == 1
    assert summary["levels"]["final"] == {
        "bugs_found": 1,
        "on_clean_diffs": 0,
        "elsewhere_on_bug_diffs": 0,
    }
    assert summary["dropped_by_reason"] == {"not_an_added_line": 1, "unsupported": 2}
    assert "| final | 1 | 0 | 0 |" in table_markdown(summary)


def test_a_failed_case_is_reported_and_three_failures_in_a_row_stop_the_run():
    boom = LLMError("busy")
    cases = [BUG, CLEAN, BUG, CLEAN, BUG]
    reviewer = FakeReviewer([ReviewResult(), boom, boom, boom, ReviewResult()])

    records = run_cases(reviewer, cases)
    summary = summarize(records, cases)

    assert reviewer.calls == 4  # case 1 ok, then three failures in a row: the 5th is never run
    assert summary["failed_cases"] == 4  # the 3 that failed AND the one never run: not hidden
    assert "INCOMPLETE" in table_markdown(summary)


def test_comments_that_could_not_be_checked_are_counted():
    unchecked = Comment("a.py", 5, "high", "x", checked=False)
    records = [CaseRecord("bug1", "bug", levels={lv: [asdict(unchecked)] for lv in LEVELS})]

    assert summarize(records, [BUG])["unchecked_comments"] == 1


def test_resuming_keeps_finished_cases_and_only_reviews_the_rest():
    first = FakeReviewer([ReviewResult(), LLMError("busy"), LLMError("busy"), LLMError("busy")])
    cases = [
        replace(BUG, id="b1"),
        replace(CLEAN, id="c1"),
        replace(BUG, id="b2"),
        replace(CLEAN, id="c2"),
    ]
    done_before = run_cases(first, cases)
    second = FakeReviewer([ReviewResult(), ReviewResult(), ReviewResult()])

    records = run_cases(second, cases, previous=done_before)

    assert second.calls == 3  # the first case was not reviewed again
    assert [r.case_id for r in records] == ["b1", "c1", "b2", "c2"]
    assert all(r.levels for r in records)


def test_an_unreadable_review_resume_file_is_a_clean_error(tmp_path):
    from codelens.evaluation.reviews import load_previous_records

    broken = tmp_path / "x.json"
    broken.write_text('{"nothing": 1}')
    for path in (tmp_path / "missing.json", broken):
        with pytest.raises(DatasetError, match="cannot read"):
            load_previous_records(path)
    assert load_previous_records(None) is None


def test_duplicate_review_case_ids_are_rejected(tmp_path):
    with pytest.raises(DatasetError, match="Duplicate case ids"):
        load_review_cases(write_cases(tmp_path, [case_dict(), case_dict()]))


def test_the_table_says_when_final_comments_were_never_self_checked():
    unchecked = asdict(Comment("a.py", 5, "high", "x", checked=False))
    records = [CaseRecord("bug1", "bug", levels={lv: [unchecked] for lv in LEVELS})]

    table = table_markdown(summarize(records, [BUG]))

    assert "could not be self-checked" in table
