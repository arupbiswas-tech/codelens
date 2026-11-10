"""Answering tests: prompt, citation checking, and the ask() flow, all with fakes."""

import pytest

from codelens import answering
from codelens.answering import (
    NOT_FOUND_ANSWER,
    Answerer,
    build_prompt,
    extract_citations,
)
from codelens.config import Settings
from codelens.errors import (
    InvalidInputError,
    LLMNotConfiguredError,
    NotFoundError,
    RerankError,
)
from codelens.main import build_answerer, build_llm
from codelens.retrieval import Hit


def hit(chunk_id, path="a.py", start=10, end=20, symbol="func") -> Hit:
    return Hit(chunk_id, path, symbol, "function", start, end, f"code of {path}")


# ---- prompt ---------------------------------------------------------------------------


def test_prompt_labels_every_snippet_with_its_citation():
    hits = [hit(1, "src/auth.py", 10, 42, "login"), hit(2, "b.py")]
    prompt = build_prompt("Where is auth?", hits)

    assert "Question: Where is auth?" in prompt
    assert "### [src/auth.py:L10-L42] (login)" in prompt
    assert "### [b.py:L10-L20] (func)" in prompt
    assert "code of src/auth.py" in prompt
    assert prompt.rstrip().endswith("[path:L1-L2].")  # the citation reminder comes last


def test_prompt_omits_the_symbol_when_there_is_none():
    assert "### [a.py:L10-L20]\n" in build_prompt("q", [hit(1, symbol=None)])


# ---- citation checking ----------------------------------------------------------------


def test_citations_matching_retrieved_chunks_are_verified():
    hits = [hit(1, "a.py", 10, 20), hit(2, "b.py", 5, 9)]

    verified, ungrounded = extract_citations("See [a.py:L10-L20] and [b.py:L5-L9].", hits)

    assert [h.id for h in verified] == [1, 2]
    assert ungrounded == 0


def test_a_citation_of_a_made_up_file_is_counted_as_ungrounded():
    verified, ungrounded = extract_citations("It is in [ghost.py:L1-L5].", [hit(1)])

    assert verified == []
    assert ungrounded == 1


def test_a_citation_of_lines_outside_the_chunk_is_ungrounded():
    verified, ungrounded = extract_citations("[a.py:L100-L120]", [hit(1, "a.py", 10, 20)])

    assert (verified, ungrounded) == ([], 1)


def test_a_cited_sub_range_or_single_line_still_counts():
    hits = [hit(1, "a.py", 10, 20)]

    assert [h.id for h in extract_citations("[a.py:L12-L15]", hits)[0]] == [1]
    assert [h.id for h in extract_citations("[a.py:L12]", hits)[0]] == [1]
    assert [h.id for h in extract_citations("[a.py:L12-15]", hits)[0]] == [1]


def test_the_same_chunk_cited_twice_is_listed_once_in_order_of_first_mention():
    hits = [hit(1, "a.py", 10, 20), hit(2, "b.py", 1, 2)]

    verified, _ = extract_citations("[b.py:L1-L2] then [a.py:L10-L20] then [b.py:L1-L2]", hits)

    assert [h.id for h in verified] == [2, 1]


def test_citations_work_with_paths_in_folders_and_without_brackets():
    hits = [hit(1, "src/pkg/mod-1.py", 3, 9)]

    assert extract_citations("see src/pkg/mod-1.py:L3-L9 for details", hits)[0] == hits


def test_answer_without_citations_has_none():
    assert extract_citations("No references here.", [hit(1)]) == ([], 0)


# ---- the ask() flow -------------------------------------------------------------------


class FakeEmbedder:
    def embed(self, texts):
        return [[0.0] * 3 for _ in texts]


class FakeReranker:
    def __init__(self, fail=False):
        self.fail = fail

    def rerank(self, question, hits, top_n):
        if self.fail:
            raise RerankError("down")
        return list(reversed(hits))[:top_n]


class FakeLLM:
    def __init__(self, reply="Answer [a.py:L10-L20]."):
        self.reply = reply
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.reply


@pytest.fixture
def searches(monkeypatch):
    """Replace the database lookups; `state` lets a test choose what they return."""
    state = {"repo_id": 1, "hits": [hit(1, "a.py", 10, 20), hit(2, "b.py", 1, 5)], "asked": []}
    monkeypatch.setattr(answering, "find_repo_id", lambda pool, name: state["repo_id"])

    def fake_search(pool, repo_id, question, vector, per_search, limit):
        state["asked"].append((repo_id, question, per_search, limit))
        return state["hits"]

    monkeypatch.setattr(answering, "hybrid_search", fake_search)
    return state


def make_answerer(llm="default", reranker="default", **settings) -> Answerer:
    return Answerer(
        pool=None,
        embedder=FakeEmbedder(),
        reranker=FakeReranker() if reranker == "default" else reranker,
        llm=FakeLLM() if llm == "default" else llm,
        settings=Settings(database_url="postgresql://unused", **settings),
    )


def test_ask_returns_the_answer_with_verified_citations(searches):
    llm = FakeLLM("Login is in [a.py:L10-L20] but see also [nope.py:L1-L2].")

    answer = make_answerer(llm=llm).ask("o/r", "  Where is login?  ")

    assert answer.text.startswith("Login is in")
    assert [h.id for h in answer.citations] == [1]
    assert answer.ungrounded_citations == 1
    assert "Question: Where is login?" in llm.calls[0][1]  # trimmed question reached the prompt


def test_ask_uses_the_configured_limits(searches):
    make_answerer(search_candidates=7, fused_candidates=4).ask("o/r", "q")

    assert searches["asked"] == [(1, "q", 7, 4)]


def test_ask_puts_only_context_chunks_in_the_prompt(searches):
    searches["hits"] = [hit(i, f"f{i}.py") for i in range(1, 6)]
    llm = FakeLLM()

    make_answerer(llm=llm, context_chunks=2).ask("o/r", "q")

    prompt = llm.calls[0][1]
    assert prompt.count("### [") == 2
    assert "f5.py" in prompt and "f4.py" in prompt  # FakeReranker reverses: best two are last


def test_ask_without_a_reranker_uses_the_merged_order(searches):
    searches["hits"] = [hit(i, f"f{i}.py") for i in range(1, 4)]
    llm = FakeLLM()

    make_answerer(llm=llm, reranker=None, context_chunks=2).ask("o/r", "q")

    assert "f1.py" in llm.calls[0][1] and "f3.py" not in llm.calls[0][1]


def test_a_failing_reranker_does_not_fail_the_question(searches):
    answer = make_answerer(reranker=FakeReranker(fail=True)).ask("o/r", "q")

    assert answer.text  # still answered, using the merged search order


def test_nothing_found_means_no_llm_call(searches):
    searches["hits"] = []
    llm = FakeLLM()

    answer = make_answerer(llm=llm).ask("o/r", "q")

    assert answer.text == NOT_FOUND_ANSWER
    assert llm.calls == []


def test_unknown_repo_is_a_404_before_any_search(searches):
    searches["repo_id"] = None

    with pytest.raises(NotFoundError, match="not indexed"):
        make_answerer().ask("nobody/nothing", "q")
    assert searches["asked"] == []


def test_missing_llm_key_fails_before_any_work(searches):
    with pytest.raises(LLMNotConfiguredError):
        make_answerer(llm=None).ask("o/r", "q")
    assert searches["asked"] == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 1001])
def test_bad_questions_are_rejected(searches, question):
    with pytest.raises(InvalidInputError):
        make_answerer().ask("o/r", question)


# ---- startup wiring -------------------------------------------------------------------


def test_the_app_starts_without_an_api_key_but_cannot_answer():
    settings = Settings(database_url="postgresql://unused", rerank_enabled=False)
    settings.groq_api_key = None

    llm = build_llm(settings)
    answerer = build_answerer(settings, pool=None, embedder=None, llm=llm)

    assert llm is None and answerer._reranker is None
    with pytest.raises(LLMNotConfiguredError):
        answerer.ask("o/r", "q")


def test_an_empty_api_key_counts_as_missing():
    settings = Settings(database_url="postgresql://unused", groq_api_key="")

    assert build_llm(settings) is None


def test_a_set_api_key_builds_the_llm_client():
    settings = Settings(database_url="postgresql://unused", groq_api_key="k")

    assert build_llm(settings) is not None
