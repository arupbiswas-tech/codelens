"""Embedder tests with a fake model, so no download or ONNX runtime is needed."""

import numpy as np
import pytest

from codelens import embeddings
from codelens.embeddings import Embedder
from codelens.errors import EmbeddingError
from codelens.retry import MAX_ATTEMPTS


class FakeModel:
    """Stands in for fastembed's TextEmbedding. `failures` = how many calls raise first."""

    def __init__(self, dim: int = 4, failures: int = 0):
        self.dim = dim
        self.failures = failures
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("onnx exploded")
        return (np.full(self.dim, float(len(text)), dtype=np.float32) for text in texts)


def make_embedder(monkeypatch, model: FakeModel, dim: int = 4, batch_size: int = 2) -> Embedder:
    monkeypatch.setattr(embeddings, "TextEmbedding", lambda model_name: model)
    return Embedder("fake-model", expected_dim=dim, batch_size=batch_size, sleep=lambda _s: None)


def test_embed_returns_one_plain_float_list_per_text(monkeypatch):
    embedder = make_embedder(monkeypatch, FakeModel())
    vectors = embedder.embed(["a", "bbb"])
    assert vectors == [[1.0] * 4, [3.0] * 4]
    assert type(vectors[0]) is list and type(vectors[0][0]) is float


def test_texts_are_sent_in_batches(monkeypatch):
    model = FakeModel()
    embedder = make_embedder(monkeypatch, model, batch_size=2)
    model.batches.clear()  # forget the startup dimension check
    embedder.embed(["a", "b", "c", "d", "e"])
    assert [len(batch) for batch in model.batches] == [2, 2, 1]


def test_results_keep_the_input_order_even_though_batches_are_sorted_by_length(monkeypatch):
    model = FakeModel()
    embedder = make_embedder(monkeypatch, model, batch_size=2)
    model.batches.clear()
    texts = ["xxxxx", "a", "xxx", "bb", "xxxx"]

    vectors = embedder.embed(texts)

    assert [v[0] for v in vectors] == [5.0, 1.0, 3.0, 2.0, 4.0]  # one vector per text, in order
    assert model.batches == [["a", "bb"], ["xxx", "xxxx"], ["xxxxx"]]  # but sent shortest first


def test_embedding_nothing_is_a_no_op(monkeypatch):
    model = FakeModel()
    embedder = make_embedder(monkeypatch, model)
    model.batches.clear()
    assert embedder.embed([]) == []
    assert model.batches == []


def test_a_transient_failure_is_retried(monkeypatch):
    model = FakeModel()
    embedder = make_embedder(monkeypatch, model)
    model.failures = MAX_ATTEMPTS - 1
    assert embedder.embed(["abc"]) == [[3.0] * 4]


def test_persistent_failure_becomes_embedding_error(monkeypatch):
    model = FakeModel()
    embedder = make_embedder(monkeypatch, model)
    model.failures = 99
    with pytest.raises(EmbeddingError) as excinfo:
        embedder.embed(["abc"])
    assert "onnx" not in excinfo.value.message  # internals stay out of the client message


def test_wrong_model_dimension_fails_at_startup(monkeypatch):
    with pytest.raises(EmbeddingError, match=r"4-dim.*vector\(384\)"):
        make_embedder(monkeypatch, FakeModel(dim=4), dim=384)


def test_model_that_cannot_load_fails_at_startup(monkeypatch):
    def broken(model_name):
        raise ValueError("no such model")

    monkeypatch.setattr(embeddings, "TextEmbedding", broken)
    with pytest.raises(EmbeddingError, match="Could not load"):
        Embedder("nope", expected_dim=4, batch_size=2)
