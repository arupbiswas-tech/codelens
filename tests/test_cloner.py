"""Cloner tests: URL validation and git failure handling. No network needed."""

import subprocess

import pytest

from codelens.errors import InvalidInputError, RepoCloneError
from codelens.ingestion import cloner
from codelens.ingestion.cloner import clone_repo, parse_repo_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/arupbiswas-tech/codelens",
        "https://github.com/arupbiswas-tech/codelens/",
        "https://github.com/arupbiswas-tech/codelens.git",
        "  https://github.com/arupbiswas-tech/codelens  ",
    ],
)
def test_valid_urls_are_normalised(url):
    name, clean_url = parse_repo_url(url)
    assert name == "arupbiswas-tech/codelens"
    assert clean_url == "https://github.com/arupbiswas-tech/codelens"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http://github.com/owner/repo",  # not https
        "https://gitlab.com/owner/repo",  # not GitHub
        "https://github.com/owner",  # missing repo
        "https://github.com/owner/repo/tree/main",  # extra path
        "https://github.com/../repo",  # path trick
        "file:///etc/passwd",
        "--upload-pack=touch /tmp/pwned",
        "git@github.com:owner/repo.git",
    ],
)
def test_invalid_urls_are_rejected(url):
    with pytest.raises(InvalidInputError):
        parse_repo_url(url)


def fake_run(returncode=0, stdout="", stderr="", raises=None):
    def run(*_args, **_kwargs):
        if raises:
            raise raises
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


def test_clone_returns_commit_sha(monkeypatch, tmp_path):
    monkeypatch.setattr(cloner.subprocess, "run", fake_run(stdout="abc123\n"))
    assert clone_repo("https://github.com/o/r", tmp_path / "r") == "abc123"


def test_repo_not_found_gives_friendly_message(monkeypatch, tmp_path):
    stderr = "fatal: repository 'https://github.com/o/r/' not found"
    monkeypatch.setattr(cloner.subprocess, "run", fake_run(returncode=128, stderr=stderr))
    with pytest.raises(RepoCloneError, match="not found, or it is private"):
        clone_repo("https://github.com/o/r", tmp_path / "r")


def test_timeout_becomes_clone_error(monkeypatch, tmp_path):
    timeout = subprocess.TimeoutExpired(cmd="git", timeout=5)
    monkeypatch.setattr(cloner.subprocess, "run", fake_run(raises=timeout))
    with pytest.raises(RepoCloneError, match="longer than"):
        clone_repo("https://github.com/o/r", tmp_path / "r", timeout=5)


def test_oversized_repo_is_rejected(monkeypatch, tmp_path):
    def run_and_write_files(*_args, **_kwargs):
        (tmp_path / "r").mkdir(exist_ok=True)
        (tmp_path / "r" / "big.txt").write_bytes(b"x" * 2000)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="abc\n", stderr="")

    monkeypatch.setattr(cloner.subprocess, "run", run_and_write_files)
    with pytest.raises(RepoCloneError, match="larger than"):
        clone_repo("https://github.com/o/r", tmp_path / "r", max_bytes=1000)


def test_missing_git_becomes_clone_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cloner.subprocess, "run", fake_run(raises=FileNotFoundError()))
    with pytest.raises(RepoCloneError, match="not installed"):
        clone_repo("https://github.com/o/r", tmp_path / "r")


def test_clone_error_does_not_leak_raw_git_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cloner.subprocess, "run", fake_run(returncode=1, stderr="fatal: weird /internal/path")
    )
    with pytest.raises(RepoCloneError) as excinfo:
        clone_repo("https://github.com/o/r", tmp_path / "r")
    assert "/internal/path" not in excinfo.value.message


def test_real_git_clone_of_a_local_repo(tmp_path):
    """Runs real git against a throwaway local repo (the URL check is bypassed on purpose)."""
    source = tmp_path / "source"
    source.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    (source / "a.py").write_text("x = 1\n")
    subprocess.run([*git, "init", "-q"], cwd=source, check=True)
    subprocess.run([*git, "add", "."], cwd=source, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=source, check=True)

    sha = clone_repo(str(source), tmp_path / "clone")

    assert len(sha) == 40
    assert (tmp_path / "clone" / "a.py").exists()
