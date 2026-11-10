"""Storage tests against a real Postgres + pgvector (the SQL is the risky part, so no mocks).

They run when TEST_DATABASE_URL is set (`make test` sets it) and are skipped otherwise.
The `pool` fixture (see conftest.py) uses a throwaway Postgres schema, so your normal data
in the same database is never touched.
"""

import psycopg
import pytest
from helpers import FakeEmbedder, make_result

from codelens.errors import EmbeddingError
from codelens.store import save_repo


def stored(pool) -> set[tuple[str, int, str]]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT path, start_line, content FROM chunks").fetchall()
    return {tuple(row) for row in rows}


def test_first_save_embeds_and_stores_everything(pool):
    result = make_result(("a.py", "f", "def f(): ...", 1), ("b.py", "g", "def g(): ...", 1))

    summary = save_repo(pool, FakeEmbedder(), result)

    assert (summary.chunks, summary.embedded, summary.reused, summary.deleted) == (2, 2, 0, 0)
    with pool.connection() as conn:
        dims = conn.execute("SELECT vector_dims(embedding) FROM chunks").fetchall()
        sha = conn.execute("SELECT commit_sha FROM repos").fetchone()[0]
    assert dims == [(384,), (384,)]
    assert sha == "c" * 40


def test_saving_the_same_repo_again_embeds_nothing(pool):
    result = make_result(("a.py", "f", "def f(): ...", 1))
    save_repo(pool, FakeEmbedder(), result)
    embedder = FakeEmbedder()

    summary = save_repo(pool, embedder, result)

    assert (summary.embedded, summary.reused, summary.deleted) == (0, 1, 0)
    assert embedder.embedded_texts == []
    assert len(stored(pool)) == 1


def test_edited_code_is_replaced_and_only_it_is_re_embedded(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "old", 1), ("a.py", "g", "same", 5)))
    embedder = FakeEmbedder()

    summary = save_repo(
        pool, embedder, make_result(("a.py", "f", "new", 1), ("a.py", "g", "same", 5))
    )

    assert (summary.embedded, summary.reused, summary.deleted) == (1, 1, 1)
    assert stored(pool) == {("a.py", 1, "new"), ("a.py", 5, "same")}  # the old "old" is gone
    assert embedder.embedded_texts == ["a.py\nf\nnew"]


def test_code_that_moved_to_other_lines_keeps_its_embedding(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "def f(): ...", 1)))
    embedder = FakeEmbedder()

    summary = save_repo(pool, embedder, make_result(("a.py", "f", "def f(): ...", 40)))

    assert (summary.embedded, summary.reused, summary.deleted) == (0, 1, 1)
    assert stored(pool) == {("a.py", 40, "def f(): ...")}  # citation points at the new line


def test_deleted_files_are_removed(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1), ("b.py", "g", "y", 1)))

    summary = save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1)))

    assert summary.deleted == 1
    assert {path for path, _, _ in stored(pool)} == {"a.py"}


def test_a_repo_with_no_chunks_clears_its_old_chunks(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1)))

    save_repo(pool, FakeEmbedder(), make_result())

    assert stored(pool) == set()


def test_identical_code_in_two_places_is_embedded_once(pool):
    embedder = FakeEmbedder()

    summary = save_repo(
        pool, embedder, make_result(("a.py", "f", "same", 1), ("a.py", "f", "same", 9))
    )

    assert (summary.chunks, summary.embedded) == (2, 1)
    assert len(stored(pool)) == 2


def test_repos_do_not_touch_each_others_chunks(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1), name="o/one"))

    save_repo(pool, FakeEmbedder(), make_result(("b.py", "g", "y", 1), name="o/two"))

    assert {path for path, _, _ in stored(pool)} == {"a.py", "b.py"}


def test_embedding_failure_leaves_the_database_unchanged(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "old", 1)))

    with pytest.raises(EmbeddingError):
        save_repo(pool, FakeEmbedder(fail=True), make_result(("a.py", "f", "new", 1)))

    assert stored(pool) == {("a.py", 1, "old")}


def test_a_failure_during_writing_rolls_everything_back(pool):
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "old", 1)))
    broken = make_result(("a.py", "f", "new", 1))
    object.__setattr__(broken.chunks[0], "kind", "not-a-valid-kind")  # violates the CHECK

    with pytest.raises(psycopg.errors.CheckViolation):
        save_repo(pool, FakeEmbedder(), broken)

    assert stored(pool) == {("a.py", 1, "old")}


def test_path_and_symbol_are_searchable_with_full_text_search(pool):
    save_repo(pool, FakeEmbedder(), make_result(("src/auth.py", "Greeter.hello", "pass", 1)))

    with pool.connection() as conn:
        found = conn.execute(
            "SELECT count(*) FROM chunks WHERE fts @@ plainto_tsquery('simple', 'auth greeter')"
        ).fetchone()[0]

    assert found == 1


def test_list_repos_reports_each_repo_with_its_chunk_count_and_hides_eval_copies(pool):
    from codelens.store import list_repos

    save_repo(
        pool,
        FakeEmbedder(),
        make_result(("a.py", "f", "x", 1), ("b.py", "g", "y", 1), name="o/zeta"),
    )
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1), name="o/alpha"))
    save_repo(pool, FakeEmbedder(), make_result(("a.py", "f", "x", 1), name="o/alpha@eval-small"))
    save_repo(pool, FakeEmbedder(), make_result(name="o/empty"))

    repos = list_repos(pool)

    assert [(r.name, r.chunks) for r in repos] == [("o/alpha", 1), ("o/empty", 0), ("o/zeta", 2)]
    assert repos[0].url == "https://github.com/o/alpha"
    assert repos[0].commit_sha == "c" * 40
