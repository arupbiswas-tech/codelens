"""Reranker tests with a fake cross-encoder (no model download)."""

import pytest

from codelens import reranker
from codelens.errors import RerankError
from codelens.reranker import BATCH_SIZE, Reranker
from codelens.retrieval import Hit
from codelens.retry import MAX_ATTEMPTS


def hit(chunk_id: int, content: str) -> Hit:
    return Hit(chunk_id, f"f{chunk_id}.py", None, "function", 1, 2, content)


class FakeCrossEncoder:
    """Scores a document by how many times 'good' appears in it."""

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.seen_documents: list[str] = []
        self.batch_size = None

    def rerank(self, question, documents, batch_size):
        self.seen_documents = list(documents)
        self.batch_size = batch_size
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("onnx exploded")
        return iter([float(doc.count("good")) for doc in documents])


def make_reranker(monkeypatch, model: FakeCrossEncoder) -> Reranker:
    monkeypatch.setattr(reranker, "TextCrossEncoder", lambda model_name: model)
    return Reranker("fake", sleep=lambda _s: None)


def test_best_scoring_hits_come_first_and_top_n_is_respected(monkeypatch):
    hits = [hit(1, "meh"), hit(2, "good good"), hit(3, "good"), hit(4, "bad")]

    ranked = make_reranker(monkeypatch, FakeCrossEncoder()).rerank("q", hits, top_n=2)

    assert [h.id for h in ranked] == [2, 3]


def test_texts_are_scored_shortest_first_in_small_batches(monkeypatch):
    model = FakeCrossEncoder()
    hits = [hit(1, "x" * 50), hit(2, "x" * 5), hit(3, "x" * 20)]

    make_reranker(monkeypatch, model).rerank("q", hits, top_n=3)

    lengths = [len(doc) for doc in model.seen_documents]
    assert lengths == sorted(lengths)
    assert model.batch_size == BATCH_SIZE


def test_the_model_sees_path_and_symbol_not_only_code(monkeypatch):
    model = FakeCrossEncoder()
    chunk = Hit(1, "auth/service.py", "AuthService.login", "method", 1, 2, "return True")

    make_reranker(monkeypatch, model).rerank("q", [chunk], top_n=1)

    assert model.seen_documents == ["auth/service.py\nAuthService.login\nreturn True"]


def test_no_hits_means_no_model_call(monkeypatch):
    model = FakeCrossEncoder()

    assert make_reranker(monkeypatch, model).rerank("q", [], top_n=3) == []
    assert model.seen_documents == []


def test_a_transient_failure_is_retried(monkeypatch):
    model = FakeCrossEncoder(failures=MAX_ATTEMPTS - 1)

    ranked = make_reranker(monkeypatch, model).rerank("q", [hit(1, "good")], top_n=1)

    assert [h.id for h in ranked] == [1]


def test_a_persistent_failure_becomes_rerank_error(monkeypatch):
    model = FakeCrossEncoder(failures=99)

    with pytest.raises(RerankError) as excinfo:
        make_reranker(monkeypatch, model).rerank("q", [hit(1, "good")], top_n=1)

    assert "onnx" not in excinfo.value.message


def test_a_model_that_cannot_load_fails_at_startup(monkeypatch):
    def broken(model_name):
        raise ValueError("no such model")

    monkeypatch.setattr(reranker, "TextCrossEncoder", broken)

    with pytest.raises(RerankError, match="Could not load"):
        Reranker("nope")
