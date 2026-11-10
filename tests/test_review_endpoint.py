"""POST /review endpoint tests: the reviewer is replaced by a fake."""

import pytest
from fastapi.testclient import TestClient

from codelens.errors import (
    DiffFetchError,
    DiffTooLargeError,
    LLMNotConfiguredError,
    NotFoundError,
    ReviewFailedError,
)
from codelens.main import app
from codelens.review import github
from codelens.review.models import Comment, DroppedComment, ReviewResult


class FakeReviewer:
    def __init__(self, result=None, error=None):
        self.result = result or ReviewResult()
        self.error = error
        self.calls = []

    def run(self, repo, diff):
        self.calls.append((repo, diff))
        if self.error:
            raise self.error
        return self.result


def client_with(reviewer) -> TestClient:
    app.state.reviewer = reviewer
    return TestClient(app, raise_server_exceptions=False)


def test_a_diff_review_returns_comments_and_statistics():
    result = ReviewResult(
        comments=[Comment("a.py", 12, "high", "Bug.", evidence=("b.py:L1-L9",))],
        dropped=[
            DroppedComment(Comment("a.py", 99, "low", "x"), "not_an_added_line"),
            DroppedComment(Comment("a.py", 5, "low", "y"), "not_an_added_line"),
            DroppedComment(Comment("a.py", 6, "low", "z"), "unsupported"),
        ],
        files_reviewed=1,
        files_skipped={"not_code": 2},
        errors=["c.py: review failed (busy)"],
        partial=True,
    )
    reviewer = FakeReviewer(result)

    response = client_with(reviewer).post("/review", json={"repo": "o/r", "diff": "DIFF"})

    assert response.status_code == 200
    assert response.json() == {
        "comments": [
            {
                "path": "a.py",
                "line": 12,
                "severity": "high",
                "body": "Bug.",
                "evidence": ["b.py:L1-L9"],
                "checked": True,
                "note": "",
            }
        ],
        "files_reviewed": 1,
        "files_skipped": {"not_code": 2},
        "dropped": {"not_an_added_line": 2, "unsupported": 1},
        "errors": ["c.py: review failed (busy)"],
        "partial": True,
    }
    assert reviewer.calls == [("o/r", "DIFF")]


def test_a_pr_url_is_fetched_and_its_repo_is_used(monkeypatch):
    monkeypatch.setattr(github, "fetch_diff", lambda url, max_bytes: f"diff of {url}")
    reviewer = FakeReviewer()

    response = client_with(reviewer).post(
        "/review", json={"pr_url": "https://github.com/o/r/pull/7"}
    )

    assert response.status_code == 200
    assert reviewer.calls == [("o/r", "diff of https://github.com/o/r/pull/7.diff")]


@pytest.mark.parametrize(
    "body",
    [
        {},  # neither
        {"repo": "o/r"},  # no diff
        {"diff": "x"},  # no repo
        {"repo": "o/r", "diff": "x", "pr_url": "https://github.com/o/r/pull/1"},  # both
        {"pr_url": "https://example.com/o/r/pull/1"},  # not a GitHub PR URL
    ],
)
def test_bad_requests_get_the_standard_422(body):
    reviewer = FakeReviewer()

    response = client_with(reviewer).post("/review", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert reviewer.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFoundError("'o/r' is not indexed."), 404, "not_found"),
        (DiffTooLargeError("too big"), 422, "diff_too_large"),
        (DiffFetchError("GitHub unreachable"), 502, "diff_fetch_failed"),
        (ReviewFailedError("all files failed"), 502, "review_failed"),
        (LLMNotConfiguredError("Set GROQ_API_KEY."), 503, "llm_not_configured"),
    ],
)
def test_review_errors_use_the_standard_error_shape(error, status, code):
    response = client_with(FakeReviewer(error=error)).post(
        "/review", json={"repo": "o/r", "diff": "x"}
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
