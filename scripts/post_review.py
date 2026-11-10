"""Post a CodeLens review to a GitHub pull request. Standard library only.

Used by .github/workflows/codelens-review.yml:

    python3 scripts/post_review.py

It reads the pull request diff from GitHub, sends it to a running CodeLens server
(POST /review), and posts the result as ONE pull request review with inline comments.

To try it without GitHub (reads a diff file, prints what it would post):

    python3 scripts/post_review.py --dry-run --diff-file change.diff \\
        --repo owner/repo --codelens-url http://localhost:8000

Design rules:
  - It never fails the pull request check. If CodeLens or GitHub misbehaves, it explains in the
    log and the job summary, and exits 0: a review helper must not block merging.
  - It never posts the same comment twice: every comment carries a hidden fingerprint, and
    comments already on the pull request are skipped, so pushing again does not repeat them.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_API = "https://api.github.com"
CODELENS_TIMEOUT = 300  # a review makes about two LLM calls per file
GITHUB_TIMEOUT = 30
MAX_COMMENT_CHARS = 60_000  # GitHub rejects bodies over 65,536 characters
MAX_COMMENT_PAGES = 5  # existing comments are read 100 at a time
MARKER_PREFIX = "<!-- codelens:"

SEVERITY_LABEL = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


# ---- one small HTTP helper (tests replace it) ----------------------------------------


def http_request(method, url, headers=None, payload=None, timeout=GITHUB_TIMEOUT):
    """Send a request and return (status code, parsed JSON or text). Never raises on HTTP errors.

    A network failure becomes status 0, so callers handle "unreachable" and "rejected" the
    same way: by looking at the status.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read())
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, str(error)


def _parse(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# ---- formatting ----------------------------------------------------------------------


def fingerprint(path: str, line: int, body: str) -> str:
    """A short stable id of a comment, to recognise it on the next run."""
    return hashlib.sha1(f"{path}\0{line}\0{body}".encode()).hexdigest()[:12]


def comment_body(comment: dict) -> str:
    """The Markdown shown on the pull request for one CodeLens comment."""
    severity = SEVERITY_LABEL.get(comment["severity"], comment["severity"].upper())
    parts = [f"**{severity}**: {comment['body']}"]
    if comment.get("note"):
        parts.append(f"> Checker note: {comment['note']}")
    if comment.get("evidence"):
        refs = ", ".join(f"`{ref}`" for ref in comment["evidence"])
        parts.append(f"Related code: {refs}")
    footer = "<sub>CodeLens review"
    if not comment.get("checked", True):
        footer += " · not self-checked"
    parts.append(footer + "</sub>")
    marker = f"{MARKER_PREFIX}{fingerprint(comment['path'], comment['line'], comment['body'])} -->"
    return truncate("\n\n".join(parts)) + "\n" + marker


def truncate(text: str) -> str:
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + "\n\n(truncated)"


def summary(result: dict, posted: int, skipped_duplicates: int) -> str:
    """The text of the review itself: what was reviewed and what the checks did."""
    lines = ["### CodeLens review", ""]
    lines.append(
        f"{posted} new comment(s) on {result['files_reviewed']} reviewed file(s)."
        + (f" {skipped_duplicates} already posted earlier." if skipped_duplicates else "")
    )
    skipped = ", ".join(
        f"{n} {why.replace('_', ' ')}" for why, n in result["files_skipped"].items()
    )
    if skipped:
        lines.append(f"Not reviewed: {skipped}.")
    dropped = ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in result["dropped"].items())
    if dropped:
        lines.append(f"Rejected by CodeLens's own checks: {dropped}.")
    if result["partial"]:
        lines.append(
            "**The review stopped early** (file or time limit), so some files were not reviewed."
        )
    for problem in result["errors"]:
        lines.append(f"- Note: {problem}")
    return "\n".join(lines)


def fallback_body(result: dict, comments: list[dict]) -> str:
    """One ordinary comment listing every finding, for when inline comments are rejected."""
    lines = [summary(result, len(comments), 0), ""]
    for comment in comments:
        lines.append(f"- `{comment['path']}` line {comment['line']}: {comment_body(comment)}")
    return truncate("\n".join(lines))


# ---- the steps -----------------------------------------------------------------------


def github_headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "codelens-review-action",
    }


def fetch_diff(api: str, repo: str, pr: int, token: str) -> str | None:
    status, body = http_request(
        "GET",
        f"{api}/repos/{repo}/pulls/{pr}",
        github_headers(token, accept="application/vnd.github.v3.diff"),
    )
    if status != 200 or not isinstance(body, str):
        say_warning(f"could not read the pull request diff from GitHub (status {status})")
        return None
    return body


def run_review(codelens_url: str, token: str, repo: str, diff: str) -> dict | None:
    """Ask CodeLens to review the diff. Returns its JSON, or None after explaining why not."""
    headers = {"Content-Type": "application/json", "User-Agent": "codelens-review-action"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_request(
        "POST",
        f"{codelens_url.rstrip('/')}/review",
        headers,
        {"repo": repo, "diff": diff},
        CODELENS_TIMEOUT,
    )
    if status == 200 and isinstance(body, dict):
        return body
    if status == 0:
        say_warning(f"could not reach CodeLens at {codelens_url}: {body}")
    else:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        say_warning(f"CodeLens answered {status}: {error.get('message', 'no details')}")
    return None


def existing_fingerprints(api: str, repo: str, pr: int, token: str) -> set[str]:
    """The fingerprints of CodeLens comments that are already on the pull request."""
    found: set[str] = set()
    for page in range(1, MAX_COMMENT_PAGES + 1):
        status, body = http_request(
            "GET",
            f"{api}/repos/{repo}/pulls/{pr}/comments?per_page=100&page={page}",
            github_headers(token),
        )
        if status != 200 or not isinstance(body, list):
            break  # worst case we post a comment twice; that is better than posting none
        for comment in body:
            text = comment.get("body") or ""
            if MARKER_PREFIX in text:
                found.add(text.split(MARKER_PREFIX, 1)[1].split(" ", 1)[0])
        if len(body) < 100:
            break
    return found


def post_review(api, repo, pr, token, head_sha, result, new_comments, duplicates) -> bool:
    """Post one review with inline comments. If GitHub rejects the inline positions, post a
    single ordinary comment instead so the findings are not lost."""
    inline = [
        {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": comment_body(c)}
        for c in new_comments
    ]
    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": summary(result, len(new_comments), duplicates),
        "comments": inline,
    }
    status, body = http_request(
        "POST", f"{api}/repos/{repo}/pulls/{pr}/reviews", github_headers(token), payload
    )
    if status in (200, 201):
        return True
    say_warning(
        f"GitHub did not accept the inline review (status {status}); posting a plain comment"
    )

    status, _ = http_request(
        "POST",
        f"{api}/repos/{repo}/issues/{pr}/comments",
        github_headers(token),
        {"body": fallback_body(result, new_comments)},
    )
    if status in (200, 201):
        return True
    say_warning(f"could not post the comment either (status {status})")
    return False


# ---- output helpers ------------------------------------------------------------------


def say_warning(message: str) -> None:
    """A warning that shows in the Actions log (and is plain text elsewhere)."""
    print(f"::warning::CodeLens: {message}")


def write_job_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


# ---- main ----------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Post a CodeLens review to a pull request.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pr", type=int, default=int(os.environ.get("PR_NUMBER") or 0))
    parser.add_argument("--head-sha", default=os.environ.get("PR_HEAD_SHA"))
    parser.add_argument("--codelens-url", default=os.environ.get("CODELENS_URL"))
    parser.add_argument("--api", default=os.environ.get("GITHUB_API_URL", GITHUB_API))
    parser.add_argument("--diff-file", type=Path, help="read the diff from a file (with --dry-run)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be posted; call no GitHub API"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    github_token = os.environ.get("GITHUB_TOKEN", "")
    codelens_token = os.environ.get("CODELENS_TOKEN", "")

    if not args.codelens_url or not args.repo:
        print("::notice::CodeLens review skipped: CODELENS_URL (and the repository) must be set.")
        return 0  # for example a pull request from a fork, where secrets are not available
    if not args.dry_run and (not github_token or not args.pr or not args.head_sha):
        print(
            "::notice::CodeLens review skipped: GITHUB_TOKEN, the pull request number "
            "and its head commit are required."
        )
        return 0

    if args.dry_run:
        if not args.diff_file:
            print("--dry-run needs --diff-file", file=sys.stderr)
            return 2
        diff = args.diff_file.read_text(encoding="utf-8")
    else:
        diff = fetch_diff(args.api, args.repo, args.pr, github_token)
        if diff is None:
            return 0

    result = run_review(args.codelens_url, codelens_token, args.repo, diff)
    if result is None:
        write_job_summary(
            "### CodeLens review\nSkipped: the CodeLens server could not review this pull request."
        )
        return 0

    comments = result["comments"]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "summary": summary(result, len(comments), 0),
                    "comments": [
                        {
                            "path": c["path"],
                            "line": c["line"],
                            "side": "RIGHT",
                            "body": comment_body(c),
                        }
                        for c in comments
                    ],
                },
                indent=2,
            )
        )
        return 0

    seen = existing_fingerprints(args.api, args.repo, args.pr, github_token)
    new = [c for c in comments if fingerprint(c["path"], c["line"], c["body"]) not in seen]
    duplicates = len(comments) - len(new)

    if not new:
        text = summary(result, 0, duplicates) + "\n\nNothing new to post."
        print(text)
        write_job_summary(text)
        return 0

    posted = post_review(
        args.api, args.repo, args.pr, github_token, args.head_sha, result, new, duplicates
    )
    write_job_summary(
        summary(result, len(new), duplicates) + ("" if posted else "\n\n(could not post)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
