"""Tests for turning a request into (repo, diff): validation and fetching a PR diff."""

import io
import urllib.error

import pytest

from codelens.errors import DiffFetchError, DiffTooLargeError, InvalidInputError
from codelens.review import github
from codelens.review.github import fetch_diff, parse_pr_url, resolve_review_input


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/7",
        "https://github.com/o/r/pull/7/",
        "  https://github.com/o/r/pull/7  ",
    ],
)
def test_valid_pr_urls_give_the_repo_and_the_diff_url(url):
    assert parse_pr_url(url) == ("o/r", "https://github.com/o/r/pull/7.diff")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/o/r/pull/7",  # not https
        "https://gitlab.com/o/r/pull/7",
        "https://github.com/o/r/pull/7/files",  # extra path
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/pull/abc",
        "https://github.com.evil.com/o/r/pull/7",
        "file:///etc/passwd",
        "https://github.com/o/r/pull/7.diff",
    ],
)
def test_other_urls_are_rejected(url):
    with pytest.raises(InvalidInputError):
        parse_pr_url(url)


def test_exactly_one_of_diff_or_pr_url_is_required():
    for diff, pr_url in [(None, None), ("diff", "https://github.com/o/r/pull/1")]:
        with pytest.raises(InvalidInputError, match="exactly one"):
            resolve_review_input("o/r", diff, pr_url, 1000)


def test_a_diff_needs_a_repo_name():
    with pytest.raises(InvalidInputError, match="repo"):
        resolve_review_input(None, "diff text", None, 1000)
    with pytest.raises(InvalidInputError, match="repo"):
        resolve_review_input("  ", "diff text", None, 1000)


def test_a_diff_passes_through_with_a_trimmed_repo_name():
    assert resolve_review_input(" o/r ", "diff text", None, 1000) == ("o/r", "diff text")


def test_a_pr_url_gives_the_repo_from_the_url_and_the_fetched_diff(monkeypatch):
    seen = {}

    def fake_fetch(url, max_bytes):
        seen["args"] = (url, max_bytes)
        return "the diff"

    monkeypatch.setattr(github, "fetch_diff", fake_fetch)

    result = resolve_review_input("ignored/ignored", None, "https://github.com/o/r/pull/7", 500)

    assert result == ("o/r", "the diff")
    assert seen["args"] == ("https://github.com/o/r/pull/7.diff", 500)


# ---- fetching -------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def fake_urlopen(content=b"", error=None):
    def urlopen(request, timeout):
        assert timeout > 0, "a timeout is required on every network call"
        assert request.get_header("User-agent")
        if error:
            raise error
        return FakeResponse(content)

    return urlopen


def test_the_diff_text_is_returned(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen(b"diff --git a b"))

    assert fetch_diff("https://github.com/o/r/pull/1.diff", 1000) == "diff --git a b"


def test_a_diff_over_the_limit_is_rejected_without_reading_it_all(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen(b"x" * 5000))

    with pytest.raises(DiffTooLargeError):
        fetch_diff("https://github.com/o/r/pull/1.diff", 1000)


def http_error(code):
    return urllib.error.HTTPError("u", code, "msg", {}, None)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (http_error(404), "not found, or its repository is private"),
        (http_error(500), "could not provide"),
        (urllib.error.URLError("no route"), "Could not reach GitHub"),
        (TimeoutError(), "Could not reach GitHub"),
    ],
)
def test_download_failures_become_clean_errors(monkeypatch, error, message):
    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen(error=error))

    with pytest.raises(DiffFetchError, match=message) as excinfo:
        fetch_diff("https://github.com/o/r/pull/1.diff", 1000)

    assert "no route" not in excinfo.value.message  # internals stay out of the client message


def test_undecodable_bytes_do_not_crash(monkeypatch):
    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen(b"caf\xe9"))

    assert "caf" in fetch_diff("https://github.com/o/r/pull/1.diff", 1000)
