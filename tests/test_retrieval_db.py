"""Search tests against a real Postgres + pgvector (the SQL and index behaviour are the point)."""

from helpers import FakeEmbedder, make_result

from codelens.retrieval import find_repo_id, hybrid_search
from codelens.store import save_repo

EMBEDDER = FakeEmbedder()


def index(pool, *specs, name="o/r") -> int:
    save_repo(pool, EMBEDDER, make_result(*specs, name=name))
    return find_repo_id(pool, name)


def search(pool, repo_id, question, limit=5):
    vector = EMBEDDER.embed([question])[0]
    return hybrid_search(pool, repo_id, question, vector, per_search=10, limit=limit)


AUTH = ("auth.py", "login", "def login(user, password): check password hash for user", 1)
PAGES = ("pages.py", "render", "def render(page): draw html template for page", 1)
RARE = ("misc.py", "zzq_frobnicate", "def zzq_frobnicate(): return 42", 1)


def test_find_repo_id_ignores_case_and_returns_none_for_unknown(pool):
    repo_id = index(pool, AUTH, name="Owner/Repo")
    assert find_repo_id(pool, "owner/repo") == repo_id
    assert find_repo_id(pool, "nobody/nothing") is None


def test_the_chunk_closest_in_meaning_comes_first(pool):
    repo_id = index(pool, AUTH, PAGES, RARE)

    hits = search(pool, repo_id, "check the password for a user login")

    assert hits[0].path == "auth.py"


def test_an_exact_identifier_is_found_by_keyword_search(pool):
    repo_id = index(pool, AUTH, PAGES, RARE)

    hits = search(pool, repo_id, "zzq_frobnicate")

    assert hits[0].symbol == "zzq_frobnicate"


def test_results_carry_what_a_citation_needs(pool):
    repo_id = index(pool, ("src/auth.py", "login", "def login(): ...", 7))

    hit = search(pool, repo_id, "login")[0]

    assert (hit.path, hit.start_line, hit.end_line, hit.symbol) == ("src/auth.py", 7, 7, "login")
    assert hit.citation == "src/auth.py:L7-L7"


def test_only_the_asked_repo_is_searched(pool):
    mine = index(pool, AUTH, name="o/mine")
    theirs = ("theirs.py", "login", "def login(user, password): check password", 1)
    index(pool, theirs, name="o/theirs")

    paths = {hit.path for hit in search(pool, mine, "login password user")}

    assert paths == {"auth.py"}


def test_the_limit_is_respected(pool):
    specs = [(f"f{i}.py", f"fn{i}", f"def fn{i}(): shared words here", 1) for i in range(8)]
    repo_id = index(pool, *specs)

    assert len(search(pool, repo_id, "shared words here", limit=3)) == 3


def test_a_question_of_only_common_words_still_works_via_vector_search(pool):
    repo_id = index(pool, AUTH, PAGES)

    assert search(pool, repo_id, "where is the code") != []


def test_hostile_question_text_cannot_break_the_queries(pool):
    repo_id = index(pool, AUTH)

    search(pool, repo_id, "'); DROP TABLE chunks; -- | & ! ( ) :* \\ %s {}")

    assert find_repo_id(pool, "o/r") == repo_id  # tables still exist and answer


def test_a_repo_with_no_chunks_returns_nothing(pool):
    repo_id = index(pool)

    assert search(pool, repo_id, "anything") == []
