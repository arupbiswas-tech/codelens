"""Tests for reading the model's review JSON and the mechanical (no LLM) checks."""

import json

import pytest

from codelens.retrieval import Hit
from codelens.review.checks import (
    anchor_and_dedupe,
    parse_comments,
    parse_verdicts,
    verify_evidence,
)
from codelens.review.models import ChangedFile, Comment


def reply(*comments) -> str:
    return json.dumps({"comments": list(comments)})


def entry(line=11, severity="high", body="Off by one.", evidence=None) -> dict:
    return {"line": line, "severity": severity, "body": body, "evidence": evidence or []}


# ---- parsing the review reply ---------------------------------------------------------


def test_a_well_formed_reply_becomes_comments_for_the_file():
    comments = parse_comments(reply(entry(11), entry(12, "low", "Minor.")), "a.py", 5)

    assert [(c.path, c.line, c.severity) for c in comments] == [
        ("a.py", 11, "high"),
        ("a.py", 12, "low"),
    ]


def test_an_empty_list_is_a_valid_answer_for_a_correct_change():
    assert parse_comments(reply(), "a.py", 5) == []


@pytest.mark.parametrize(
    "text", ["", "no json here", "{broken", '{"other": []}', '{"comments": "x"}']
)
def test_unusable_replies_are_none_so_the_caller_can_retry(text):
    assert parse_comments(text, "a.py", 5) is None


def test_json_wrapped_in_prose_and_fences_is_still_found():
    text = "Here is my review:\n```json\n" + reply(entry()) + "\n```"

    assert len(parse_comments(text, "a.py", 5)) == 1


def test_malformed_entries_are_skipped_but_good_ones_survive():
    bad = [
        entry(line="eleven"),
        {"severity": "high", "body": "no line"},
        entry(body="   "),
        "text",
        None,
    ]

    comments = parse_comments(reply(*bad, entry(20)), "a.py", 5)

    assert [c.line for c in comments] == [20]


def test_a_line_number_sent_as_text_is_accepted_and_unknown_severity_becomes_medium():
    comments = parse_comments(reply(entry(line="14", severity="CRITICAL")), "a.py", 5)

    assert (comments[0].line, comments[0].severity) == (14, "medium")


def test_severity_is_case_insensitive_and_the_comment_count_is_capped():
    comments = parse_comments(
        reply(*[entry(i, "HIGH") for i in range(1, 9)]), "a.py", max_comments=3
    )

    assert len(comments) == 3 and comments[0].severity == "high"


def test_evidence_that_is_not_a_list_is_ignored():
    comments = parse_comments(reply(entry(evidence="a.py:L1-L2")), "a.py", 5)

    assert comments[0].evidence == ()


# ---- anchoring to added lines ---------------------------------------------------------


FILE = ChangedFile("a.py", added={11: "b = 3", 12: "c = 4"}, old_ranges=((10, 14),), rendered="")


def comment(line, body="Problem.") -> Comment:
    return Comment("a.py", line, "high", body)


def test_only_comments_on_added_lines_survive():
    kept, dropped = anchor_and_dedupe([comment(11), comment(10), comment(99)], FILE)

    assert [c.line for c in kept] == [11]
    assert [(d.comment.line, d.reason) for d in dropped] == [
        (10, "not_an_added_line"),
        (99, "not_an_added_line"),
    ]
    assert "line 10" in dropped[0].detail


def test_repeated_comments_are_dropped_but_two_different_ones_on_a_line_are_kept():
    kept, dropped = anchor_and_dedupe(
        [comment(11, "Same."), comment(11, "Same."), comment(11, "Another point.")], FILE
    )

    assert [c.body for c in kept] == ["Same.", "Another point."]
    assert [d.reason for d in dropped] == ["duplicate"]


# ---- evidence -------------------------------------------------------------------------


CONTEXT = [Hit(1, "src/db.py", "save", "function", 10, 40, "code")]


def test_real_evidence_is_kept_under_its_full_path_and_made_up_evidence_is_removed():
    commented = Comment(
        "a.py", 11, "high", "Breaks save().", evidence=("db.py:L12-L20", "ghost.py:L1-L5")
    )

    checked = verify_evidence(commented, CONTEXT)

    assert checked.evidence == ("src/db.py:L10-L40",)
    assert checked.body == commented.body


def test_a_comment_without_evidence_is_returned_unchanged():
    plain = comment(11)

    assert verify_evidence(plain, CONTEXT) is plain


# ---- verdicts -------------------------------------------------------------------------


def test_verdicts_are_read_by_comment_id():
    text = json.dumps(
        {
            "verdicts": [
                {"id": 0, "verdict": "supported", "reason": "ok"},
                {"id": 1, "verdict": "partly", "reason": "side remark is wrong"},
                {"id": 2, "verdict": "Unsupported", "reason": "wrong"},
            ]
        }
    )

    assert parse_verdicts(text, 3) == {
        0: ("supported", "ok"),
        1: ("partly", "side remark is wrong"),
        2: ("unsupported", "wrong"),
    }


def test_the_older_true_false_form_is_still_understood():
    text = json.dumps(
        {"verdicts": [{"id": 0, "supported": True}, {"id": 1, "supported": False, "reason": "x"}]}
    )

    assert parse_verdicts(text, 2) == {0: ("supported", ""), 1: ("unsupported", "x")}


def test_unusable_or_nonsense_verdict_entries_are_ignored_or_not_vouched_for():
    text = json.dumps(
        {
            "verdicts": [
                {"id": 7, "verdict": "supported"},  # no such comment
                {"id": "x", "verdict": "supported"},
                {"verdict": "supported"},  # no id
                {"id": 0},  # neither a verdict nor a supported flag
                {"id": 1, "verdict": "great"},  # an unknown word is not a vouch
            ]
        }
    )

    assert parse_verdicts(text, 2) == {1: ("unsupported", "")}


@pytest.mark.parametrize("text", ["", "nope", '{"verdicts": 3}', '{"x": []}'])
def test_a_reply_without_a_verdict_list_is_none(text):
    assert parse_verdicts(text, 2) is None
