"""Get the diff to review: either text sent by the caller or a public GitHub pull request."""

import logging
import re
import urllib.error
import urllib.request

from codelens.errors import DiffFetchError, DiffTooLargeError, InvalidInputError

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15

# Only https://github.com/<owner>/<repo>/pull/<number>. Like the clone URL check, this keeps
# the server from being pointed at other hosts or at local files.
_PR_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)/?$"
)


def resolve_review_input(
    repo: str | None, diff: str | None, pr_url: str | None, max_bytes: int
) -> tuple[str, str]:
    """Return (repo name, diff text) from whichever form the caller used.

    Exactly one of `diff` or `pr_url` must be given. With `pr_url` the repo is taken from
    the URL; with `diff` the caller must name the repo.
    """
    if (diff is None) == (pr_url is None):
        raise InvalidInputError("Send exactly one of 'diff' or 'pr_url'.")

    if pr_url is not None:
        repo_name, diff_url = parse_pr_url(pr_url)
        return repo_name, fetch_diff(diff_url, max_bytes)

    if not repo or not repo.strip():
        raise InvalidInputError("Send 'repo' (for example owner/repo) together with 'diff'.")
    return repo.strip(), diff


def parse_pr_url(url: str) -> tuple[str, str]:
    """Return ("owner/repo", "<pull request url>.diff"), or raise InvalidInputError."""
    match = _PR_URL.match(url.strip())
    if match is None:
        raise InvalidInputError("Expected a URL like https://github.com/owner/repo/pull/7")
    name = f"{match['owner']}/{match['repo']}"
    return name, f"https://github.com/{name}/pull/{match['number']}.diff"


def fetch_diff(diff_url: str, max_bytes: int) -> str:
    """Download a pull request's diff. Never reads more than `max_bytes` plus one byte."""
    request = urllib.request.Request(diff_url, headers={"User-Agent": "codelens-reviewer"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        logger.warning("Fetching %s failed with HTTP %s", diff_url, exc.code)
        if exc.code in (404, 410):
            raise DiffFetchError("Pull request not found, or its repository is private.") from exc
        raise DiffFetchError("GitHub could not provide the pull request diff.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Fetching %s failed: %s", diff_url, exc)
        raise DiffFetchError("Could not reach GitHub to download the diff.") from exc

    if len(raw) > max_bytes:
        raise DiffTooLargeError(f"The diff is larger than {max_bytes // 1000} KB.")
    return raw.decode("utf-8", errors="replace")
