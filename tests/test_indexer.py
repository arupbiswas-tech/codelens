"""Indexer, POST /repos and POST /ask endpoint tests. Everything slow is replaced by fakes."""

import pytest
from fastapi.testclient import TestClient

from codelens import indexer
from codelens.answering import Answer
from codelens.errors import LLMError, LLMNotConfiguredError, NotFoundError, RepoTooLargeError
from codelens.indexer import index_repo
from codelens.ingestion.models import Chunk, IngestResult, build_chunk
from codelens.main import app
from codelens.retrieval import Hit
from codelens.store import SaveSummary


def make_result(chunk_count: int) -> IngestResult:
    lines = ["x = 1"]
    chunks = [build_chunk("a.py", lines, 1, 1, "module") for _ in range(chunk_count)]
    return IngestResult("o/r", "https://github.com/o/r", "a" * 40, chunks, {})


def test_embedding_text_adds_path_and_symbol_but_content_stays_raw():
    chunk = Chunk("pkg/auth.py", "method", 3, 4, "def login(): ...", "h", symbol="Auth.login")
    assert chunk.embedding_text == "pkg/auth.py\nAuth.login\ndef login(): ..."
    assert chunk.content == "def login(): ..."


def test_embedding_text_without_symbol_has_only_the_path():
    chunk = Chunk("README.md", "window", 1, 2, "# Title", "h")
    assert chunk.embedding_text == "README.md\n# Title"


def test_too_many_chunks_is_rejected_before_embedding(monkeypatch):
    monkeypatch.setattr(indexer, "ingest_repo", lambda url, clone_timeout: make_result(6))

    def must_not_run(*_args):
        raise AssertionError("must not embed or store an oversized repo")

    monkeypatch.setattr(indexer, "save_repo", must_not_run)
    with pytest.raises(RepoTooLargeError, match="limit is 5"):
        index_repo("https://github.com/o/r", None, None, max_chunks=5, clone_timeout=1)


def test_index_repo_returns_the_save_summary(monkeypatch):
    summary = SaveSummary(chunks=2, embedded=1, reused=1, deleted=0)
    monkeypatch.setattr(indexer, "ingest_repo", lambda url, clone_timeout: make_result(2))
    monkeypatch.setattr(indexer, "save_repo", lambda pool, embedder, result: summary)

    result = index_repo("https://github.com/o/r", None, None, max_chunks=5, clone_timeout=1)

    assert (result.repo, result.summary) == ("o/r", summary)


@pytest.fixture
def client() -> TestClient:
    app.state.pool = object()
    app.state.embedder = object()
    return TestClient(app, raise_server_exceptions=False)


def test_post_repos_returns_the_summary(client, monkeypatch):
    summary = SaveSummary(chunks=10, embedded=4, reused=6, deleted=1)
    fake = indexer.IndexResult(repo="o/r", commit_sha="c" * 40, summary=summary)
    monkeypatch.setattr("codelens.main.index_repo", lambda *_a, **_k: fake)

    response = client.post("/repos", json={"url": "https://github.com/o/r"})

    assert response.status_code == 200
    assert response.json() == {
        "repo": "o/r",
        "commit_sha": "c" * 40,
        "chunks": 10,
        "embedded": 4,
        "reused": 6,
        "deleted": 1,
    }


def test_post_repos_bad_url_is_a_clean_422(client):
    response = client.post("/repos", json={"url": "https://example.com/not/github"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_post_repos_missing_url_uses_the_standard_error_shape(client):
    response = client.post("/repos", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert "url" in response.json()["error"]["message"]


# ---- POST /ask ------------------------------------------------------------------------


class FakeAnswerer:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def ask(self, repo, question):
        self.calls.append((repo, question))
        if self.error:
            raise self.error
        return self.answer


def ask_client(answerer) -> TestClient:
    app.state.answerer = answerer
    return TestClient(app, raise_server_exceptions=False)


def test_ask_returns_answer_and_citations():
    hit = Hit(1, "src/auth.py", "login", "function", 10, 42, "code")
    answerer = FakeAnswerer(Answer("Login is here [src/auth.py:L10-L42]", [hit], 2))

    response = ask_client(answerer).post("/ask", json={"repo": "o/r", "question": "Where?"})

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Login is here [src/auth.py:L10-L42]",
        "citations": [{"path": "src/auth.py", "start_line": 10, "end_line": 42, "symbol": "login"}],
        "ungrounded_citations": 2,
    }
    assert answerer.calls == [("o/r", "Where?")]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFoundError("'o/r' is not indexed."), 404, "not_found"),
        (LLMError("The language model is busy."), 502, "llm_failed"),
        (LLMNotConfiguredError("Set GROQ_API_KEY."), 503, "llm_not_configured"),
    ],
)
def test_ask_errors_use_the_standard_error_shape(error, status, code):
    response = ask_client(FakeAnswerer(error=error)).post(
        "/ask", json={"repo": "o/r", "question": "q"}
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


INCOMPLETE_BODIES = [{}, {"repo": "o/r"}, {"question": "q"}, {"repo": "", "question": "q"}]


@pytest.mark.parametrize("body", INCOMPLETE_BODIES)
def test_ask_rejects_incomplete_requests(body):
    response = ask_client(FakeAnswerer()).post("/ask", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


# ---- GET /repos -----------------------------------------------------------------------


def test_get_repos_lists_indexed_repositories(monkeypatch):
    from datetime import UTC, datetime

    from codelens.store import RepoSummary

    when = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "codelens.main.list_repos",
        lambda pool: [RepoSummary("o/r", "https://github.com/o/r", "c" * 40, when, 12)],
    )
    app.state.pool = object()

    response = TestClient(app, raise_server_exceptions=False).get("/repos")

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "o/r",
            "url": "https://github.com/o/r",
            "commit_sha": "c" * 40,
            "ingested_at": "2026-11-09T12:00:00Z",
            "chunks": 12,
        }
    ]
