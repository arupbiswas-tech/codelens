"""Tests for scripts/post_review.py (the GitHub Action): GitHub and CodeLens are faked."""

import json

import post_review as pr
import pytest

REPO = "o/r"
API = "https://api.github.com"
CODELENS = "https://codelens.example"


def comment(path="a.py", line=12, severity="high", body="Off by one.", **extra) -> dict:
    return {
        "path": path,
        "line": line,
        "severity": severity,
        "body": body,
        "evidence": [],
        "checked": True,
        "note": "",
        **extra,
    }


def review_result(*comments, **overrides) -> dict:
    result = {
        "comments": list(comments),
        "files_reviewed": 1,
        "files_skipped": {},
        "dropped": {},
        "errors": [],
        "partial": False,
    }
    return {**result, **overrides}


class FakeWorld:
    """Stands in for GitHub and CodeLens. Records every request; answers from `routes`."""

    def __init__(self, review=None, existing=None, review_status=200, post_status=200):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.review = review if review is not None else review_result(comment())
        self.review_status = review_status
        self.existing = existing or []
        self.post_status = post_status  # status of POST .../reviews; issue comments use 201

    def __call__(self, method, url, headers=None, payload=None, timeout=30):
        self.calls.append((method, url, payload))
        if url.endswith("/pulls/7") and method == "GET":
            return 200, "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        if url.startswith(CODELENS):
            return self.review_status, self.review
        if "/pulls/7/comments" in url:
            return 200, self.existing
        if url.endswith("/pulls/7/reviews"):
            return self.post_status, {}
        if url.endswith("/issues/7/comments"):
            return 201, {}
        return 404, "unexpected"

    def posted(self, suffix):
        return [
            payload
            for method, url, payload in self.calls
            if method == "POST" and url.endswith(suffix)
        ]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("CODELENS_URL", CODELENS)
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("PR_HEAD_SHA", "abc123")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("CODELENS_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def run(monkeypatch, world, *argv) -> int:
    monkeypatch.setattr(pr, "http_request", world)
    return pr.main(list(argv))


# ---- formatting -----------------------------------------------------------------------


def test_the_fingerprint_is_stable_and_depends_on_path_line_and_text():
    base = pr.fingerprint("a.py", 12, "text")

    assert base == pr.fingerprint("a.py", 12, "text")
    assert len(base) == 12
    assert len({base, pr.fingerprint("b.py", 12, "text"), pr.fingerprint("a.py", 13, "text"),
                pr.fingerprint("a.py", 12, "other")}) == 4  # fmt: skip


def test_a_comment_shows_severity_text_evidence_note_and_a_hidden_marker():
    body = pr.comment_body(
        comment(evidence=["src/db.py:L10-L40"], note="the fetchall remark is not supported")
    )

    assert body.startswith("**HIGH**: Off by one.")
    assert "> Checker note: the fetchall remark is not supported" in body
    assert "Related code: `src/db.py:L10-L40`" in body
    assert body.rstrip().endswith("-->")
    assert pr.MARKER_PREFIX + pr.fingerprint("a.py", 12, "Off by one.") in body


def test_an_unchecked_comment_says_so():
    assert "not self-checked" in pr.comment_body(comment(checked=False))


def test_a_huge_comment_is_truncated_below_githubs_limit():
    body = pr.comment_body(comment(body="x" * 100_000))

    assert len(body) < 65_000 and "(truncated)" in body


def test_the_summary_reports_skips_rejections_and_early_stops():
    result = review_result(
        files_skipped={"not_code": 2},
        dropped={"unsupported": 1},
        partial=True,
        errors=["b.py: review failed (busy)"],
    )

    text = pr.summary(result, posted=3, skipped_duplicates=1)

    assert "3 new comment(s) on 1 reviewed file(s). 1 already posted earlier." in text
    assert "Not reviewed: 2 not code." in text
    assert "Rejected by CodeLens's own checks: 1 unsupported." in text
    assert "stopped early" in text and "b.py: review failed (busy)" in text


# ---- the normal run -------------------------------------------------------------------


def test_new_comments_are_posted_as_one_review_on_the_right_lines(monkeypatch, env):
    world = FakeWorld(review_result(comment(line=12), comment(path="b.py", line=3, severity="low")))

    assert run(monkeypatch, world) == 0

    [review] = world.posted("/pulls/7/reviews")
    assert review["commit_id"] == "abc123" and review["event"] == "COMMENT"
    assert [(c["path"], c["line"], c["side"]) for c in review["comments"]] == [
        ("a.py", 12, "RIGHT"),
        ("b.py", 3, "RIGHT"),
    ]
    assert "2 new comment(s)" in review["body"]


def test_the_codelens_request_carries_the_repo_diff_and_optional_token(monkeypatch, env):
    monkeypatch.setenv("CODELENS_TOKEN", "secret")
    seen = {}

    def world(method, url, headers=None, payload=None, timeout=30):
        if url.startswith(CODELENS):
            seen.update(url=url, payload=payload, headers=headers, timeout=timeout)
            return 200, review_result()
        return FakeWorld()(method, url, headers, payload, timeout)

    monkeypatch.setattr(pr, "http_request", world)
    pr.main([])

    assert seen["url"] == f"{CODELENS}/review"
    assert seen["payload"]["repo"] == REPO and seen["payload"]["diff"].startswith("diff --git")
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["timeout"] >= 120


def test_comments_that_are_already_on_the_pull_request_are_not_posted_again(monkeypatch, env):
    old = comment(line=12)
    fresh = comment(line=20, body="A different problem.")
    already = {"body": pr.comment_body(old)}
    world = FakeWorld(
        review_result(old, fresh), existing=[already, {"body": "someone else's comment"}]
    )

    run(monkeypatch, world)

    [review] = world.posted("/pulls/7/reviews")
    assert [c["line"] for c in review["comments"]] == [20]
    assert "1 already posted earlier" in review["body"]


def test_when_everything_was_posted_before_nothing_is_posted(monkeypatch, env):
    old = comment()
    world = FakeWorld(review_result(old), existing=[{"body": pr.comment_body(old)}])

    assert run(monkeypatch, world) == 0

    assert world.posted("/pulls/7/reviews") == [] and world.posted("/issues/7/comments") == []


def test_a_review_without_findings_posts_nothing(monkeypatch, env):
    world = FakeWorld(review_result())

    assert run(monkeypatch, world) == 0
    assert world.posted("/pulls/7/reviews") == []


def test_existing_comments_are_read_page_by_page(monkeypatch, env):
    pages = {1: [{"body": "x"}] * 100, 2: [{"body": pr.MARKER_PREFIX + "abc123abc123 -->"}]}

    def world(method, url, headers=None, payload=None, timeout=30):
        page = int(url.rsplit("page=", 1)[1])
        return 200, pages[page]

    monkeypatch.setattr(pr, "http_request", world)

    assert pr.existing_fingerprints(API, REPO, 7, "t") == {"abc123abc123"}


# ---- it must never fail the pull request ----------------------------------------------


def test_if_github_rejects_the_inline_review_the_findings_are_posted_as_a_plain_comment(
    monkeypatch, env, capsys
):
    world = FakeWorld(review_result(comment()), post_status=422)

    assert run(monkeypatch, world) == 0

    [plain] = world.posted("/issues/7/comments")
    assert "`a.py` line 12" in plain["body"] and "Off by one." in plain["body"]
    assert "::warning::" in capsys.readouterr().out


def test_if_codelens_is_down_nothing_is_posted_and_the_job_still_succeeds(monkeypatch, env, capsys):
    world = FakeWorld(review="connection refused", review_status=0)

    assert run(monkeypatch, world) == 0

    assert world.posted("/pulls/7/reviews") == []
    assert "could not reach CodeLens" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            404,
            {"error": {"code": "not_found", "message": "'o/r' is not indexed."}},
            "is not indexed",
        ),
        (
            502,
            {"error": {"code": "review_failed", "message": "all files failed"}},
            "all files failed",
        ),
        (503, "<html>bad gateway</html>", "no details"),
    ],
)
def test_codelens_errors_are_explained_and_do_not_fail_the_job(
    monkeypatch, env, capsys, status, body, expected
):
    assert run(monkeypatch, FakeWorld(review=body, review_status=status)) == 0

    assert expected in capsys.readouterr().out


def test_a_missing_server_url_skips_quietly_as_on_pull_requests_from_forks(
    monkeypatch, env, capsys
):
    monkeypatch.delenv("CODELENS_URL")
    world = FakeWorld()

    assert run(monkeypatch, world) == 0

    assert world.calls == [] and "skipped" in capsys.readouterr().out


def test_missing_github_details_skip_quietly(monkeypatch, env, capsys):
    monkeypatch.delenv("PR_NUMBER")
    world = FakeWorld()

    assert run(monkeypatch, world) == 0

    assert world.calls == []


def test_if_the_diff_cannot_be_read_the_job_still_succeeds(monkeypatch, env, capsys):
    def world(method, url, headers=None, payload=None, timeout=30):
        return 403, "rate limited"

    assert run(monkeypatch, world) == 0
    assert "could not read the pull request diff" in capsys.readouterr().out


def test_the_job_summary_is_written_when_github_provides_a_file(monkeypatch, env, tmp_path):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    run(monkeypatch, FakeWorld())

    assert "CodeLens review" in summary_file.read_text()


# ---- the HTTP helper ------------------------------------------------------------------


def test_network_failures_become_status_zero_instead_of_raising(monkeypatch):
    import urllib.error

    def boom(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(pr.urllib.request, "urlopen", boom)

    status, body = pr.http_request("GET", "https://x.invalid/")

    assert status == 0 and "no route" in body


# ---- dry run --------------------------------------------------------------------------


def test_a_dry_run_prints_what_would_be_posted_and_calls_no_github_api(
    monkeypatch, tmp_path, capsys
):
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("diff --git a/a.py b/a.py\n")
    world = FakeWorld()

    code = run(monkeypatch, world, "--dry-run", "--diff-file", str(diff_file), "--repo", REPO,
               "--codelens-url", CODELENS)  # fmt: skip

    assert code == 0
    assert [url for _m, url, _p in world.calls] == [f"{CODELENS}/review"]  # CodeLens only
    printed = json.loads(capsys.readouterr().out)
    assert printed["comments"][0]["path"] == "a.py" and printed["comments"][0]["side"] == "RIGHT"
    assert "CodeLens review" in printed["summary"]


def test_a_dry_run_needs_a_diff_file(monkeypatch):
    assert (
        run(monkeypatch, FakeWorld(), "--dry-run", "--repo", REPO, "--codelens-url", CODELENS) == 2
    )


# ---- a real HTTP round trip -----------------------------------------------------------


def test_the_whole_flow_over_real_http_against_a_local_stand_in_for_github_and_codelens(
    monkeypatch, env
):
    """No fakes inside the script: real urllib requests go to a local server that plays both
    GitHub and CodeLens, so headers, JSON bodies and status handling are really exercised."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[tuple[str, str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length)) if length else None
            seen.append(
                (
                    self.command,
                    self.path,
                    {"auth": self.headers.get("Authorization"), "payload": payload},
                )
            )
            return payload

        def do_GET(self):
            self._record()
            if self.path.endswith("/pulls/7"):
                self._send(200, b"diff --git a/a.py b/a.py\n", "text/plain")
            else:
                self._send(200, [])  # no earlier comments

        def do_POST(self):
            self._record()
            if self.path == "/review":
                self._send(200, review_result(comment()))
            else:
                self._send(200, {"id": 1})

        def log_message(self, *_args):  # keep the test output quiet
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("CODELENS_URL", base)
    monkeypatch.setenv("CODELENS_TOKEN", "codelens-secret")

    try:
        code = pr.main(["--api", base])
    finally:
        server.shutdown()

    assert code == 0
    by_request = {(method, path.split("?")[0]): info for method, path, info in seen}
    assert by_request[("GET", "/repos/o/r/pulls/7")]["auth"] == "Bearer gh-token"
    review_call = by_request[("POST", "/review")]
    assert review_call["auth"] == "Bearer codelens-secret"
    assert review_call["payload"]["repo"] == "o/r"
    posted = by_request[("POST", "/repos/o/r/pulls/7/reviews")]["payload"]
    assert posted["comments"][0]["path"] == "a.py" and posted["comments"][0]["line"] == 12
    assert posted["comments"][0]["body"].startswith("**HIGH**")
