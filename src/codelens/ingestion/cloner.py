"""Validate a GitHub URL and clone it with git."""

import logging
import os
import re
import subprocess
from pathlib import Path

from codelens.errors import InvalidInputError, RepoCloneError

logger = logging.getLogger(__name__)

MAX_REPO_BYTES = 200_000_000  # refuse huge repos; they would fill the temp disk

# Only public GitHub https URLs are accepted. This also blocks file://, ssh and
# option-like inputs such as "--upload-pack=...".
_GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def parse_repo_url(url: str) -> tuple[str, str]:
    """Return (name, clean_url) like ("owner/repo", "https://github.com/owner/repo").

    Raises InvalidInputError for anything that is not a plain GitHub repo URL.
    """
    match = _GITHUB_URL.match(url.strip())
    if match is None or {match["owner"], match["repo"]} & {".", ".."}:
        raise InvalidInputError("Expected a URL like https://github.com/owner/repo")
    name = f"{match['owner']}/{match['repo']}"
    return name, f"https://github.com/{name}"


def clone_repo(
    url: str, destination: Path, timeout: float = 120, max_bytes: int = MAX_REPO_BYTES
) -> str:
    """Shallow-clone `url` into `destination` and return the commit SHA that was cloned.

    `url` must already be validated (see parse_repo_url). Raises RepoCloneError if the
    checked-out files are bigger than `max_bytes`.
    """
    _run_git(
        ["clone", "--depth", "1", "--single-branch", "--quiet", "--", url, str(destination)],
        timeout=timeout,
    )
    if _working_tree_bytes(destination) > max_bytes:
        raise RepoCloneError(f"Repository is larger than {max_bytes // 1_000_000} MB; not indexed.")
    return _run_git(["-C", str(destination), "rev-parse", "HEAD"], timeout=timeout).strip()


def _working_tree_bytes(root: Path) -> int:
    """Total size of the checked-out files, ignoring .git. Symlinks count as themselves."""
    total = 0
    for dir_path, dir_names, file_names in os.walk(root):
        dir_names[:] = [d for d in dir_names if d != ".git"]
        for file_name in file_names:
            total += os.lstat(os.path.join(dir_path, file_name)).st_size
    return total


def _run_git(args: list[str], timeout: float) -> str:
    """Run a git command and return stdout. Any failure becomes a RepoCloneError."""
    # GIT_TERMINAL_PROMPT=0 makes git fail instead of waiting for a password on private repos.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoCloneError(f"Cloning took longer than {timeout:.0f}s and was stopped.") from exc
    except FileNotFoundError as exc:
        raise RepoCloneError("git is not installed in this environment.") from exc

    if result.returncode != 0:
        logger.warning("git %s failed: %s", args[0], result.stderr.strip())
        raise RepoCloneError(_friendly_git_error(result.stderr))
    return result.stdout


def _friendly_git_error(stderr: str) -> str:
    """Turn raw git output into a short message that is safe to show to a user."""
    text = stderr.lower()
    if "not found" in text or "could not read username" in text or "authentication" in text:
        return "Repository not found, or it is private."
    return "git could not clone the repository."
