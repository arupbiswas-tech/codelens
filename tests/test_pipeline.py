"""Pipeline tests: walking and chunking a directory, and cleanup after cloning."""

from pathlib import Path

import pytest

from codelens.errors import InvalidInputError, RepoCloneError
from codelens.ingestion import pipeline
from codelens.ingestion.pipeline import ingest_directory, ingest_repo


def make_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(
        "def login(user):\n    return user\n\n\ndef logout(user):\n    return None\n"
    )
    (root / "README.md").write_text("# Demo\n\nA demo repo.\n")
    (root / "logo.png").write_bytes(b"\x89PNG\x00")


def test_ingest_directory_chunks_files_and_reports_stats(tmp_path):
    make_repo(tmp_path)

    chunks, stats = ingest_directory(tmp_path)

    symbols = {c.symbol for c in chunks if c.symbol}
    assert symbols == {"login", "logout"}
    assert {c.path for c in chunks} == {"src/auth.py", "README.md"}
    assert stats["files_read"] == 2
    assert stats["skipped_file_type"] == 1
    assert stats["chunks"] == len(chunks)


def test_one_failing_file_does_not_stop_the_run(tmp_path, monkeypatch):
    make_repo(tmp_path)
    real_chunk_file = pipeline.chunk_file

    def flaky(path, text):
        if path == "README.md":
            raise RuntimeError("boom")
        return real_chunk_file(path, text)

    monkeypatch.setattr(pipeline, "chunk_file", flaky)

    chunks, stats = ingest_directory(tmp_path)

    assert stats["failed_chunking"] == 1
    assert {c.path for c in chunks} == {"src/auth.py"}


def test_ingest_repo_rejects_bad_url_before_cloning(monkeypatch):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("clone should not be attempted")

    monkeypatch.setattr(pipeline, "clone_repo", must_not_run)
    with pytest.raises(InvalidInputError):
        ingest_repo("https://example.com/not/github")


def test_ingest_repo_uses_clone_result_and_cleans_up(monkeypatch):
    seen = {}

    def fake_clone(url, destination, timeout):
        make_repo(destination)
        seen["destination"] = destination
        return "a" * 40

    monkeypatch.setattr(pipeline, "clone_repo", fake_clone)

    result = ingest_repo("https://github.com/owner/repo.git")

    assert result.repo_name == "owner/repo"
    assert result.url == "https://github.com/owner/repo"
    assert result.commit_sha == "a" * 40
    assert result.stats["chunks"] == len(result.chunks) > 0
    assert not seen["destination"].exists(), "temporary clone must be deleted"


def test_temporary_clone_is_deleted_when_cloning_fails(monkeypatch):
    seen = {}

    def failing_clone(url, destination, timeout):
        seen["destination"] = destination
        raise RepoCloneError("nope")

    monkeypatch.setattr(pipeline, "clone_repo", failing_clone)

    with pytest.raises(RepoCloneError):
        ingest_repo("https://github.com/owner/repo")
    assert not seen["destination"].parent.exists()
