"""Find the code chunks most relevant to a question: vector search + keyword search, merged.

Why two searches? They fail in opposite ways:
  - Vector search matches meaning ("where is login handled" finds `authenticate_user`)
    but blurs exact names and rare identifiers.
  - Full-text search matches exact words (`parse_repo_url`) but knows nothing about meaning.
Running both and merging their rankings keeps the strengths of each.
"""

import json
import re
from dataclasses import dataclass

from psycopg import Connection
from psycopg_pool import ConnectionPool

from codelens.db import translate_db_errors
from codelens.ingestion.models import text_for_model

RRF_K = 60  # the standard constant from the original Reciprocal Rank Fusion paper

# Words that appear in nearly every question and every chunk. Searching for them only adds noise.
STOP_WORDS = frozenset(
    "a an and are as at be by can code does do for from how i in is it its of on or the this "
    "that to was what where which who why with file function class method used use work "
    "works".split()
)

COLUMNS = "id, path, symbol, kind, start_line, end_line, content"

FIND_REPO_SQL = "SELECT id FROM repos WHERE lower(name) = lower(%s)"

VECTOR_SEARCH_SQL = f"""
    SELECT {COLUMNS} FROM chunks
    WHERE repo_id = %s AND embedding IS NOT NULL
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""

# ts_rank_cd rewards query words that appear close together; flag 1 divides by log(length)
# so long chunks do not win just by containing more words.
KEYWORD_SEARCH_SQL = f"""
    SELECT {COLUMNS} FROM chunks
    WHERE repo_id = %s AND fts @@ to_tsquery('simple', %s)
    ORDER BY ts_rank_cd(fts, to_tsquery('simple', %s), 1) DESC
    LIMIT %s
"""


@dataclass(frozen=True)
class Hit:
    """One chunk returned by a search."""

    id: int
    path: str
    symbol: str | None
    kind: str
    start_line: int
    end_line: int
    content: str

    @property
    def citation(self) -> str:
        return f"{self.path}:L{self.start_line}-L{self.end_line}"

    @property
    def text(self) -> str:
        """What the reranker and the prompt see: the code plus its path and symbol."""
        return text_for_model(self.path, self.symbol, self.content)


def find_repo_id(pool: ConnectionPool, repo_name: str) -> int | None:
    """Return the id of an indexed repo (name match ignores case), or None."""
    with translate_db_errors("looking up a repo"), pool.connection() as conn:
        row = conn.execute(FIND_REPO_SQL, (repo_name,)).fetchone()
    return row[0] if row else None


def hybrid_search(
    pool: ConnectionPool,
    repo_id: int,
    question: str,
    question_vector: list[float],
    per_search: int,
    limit: int,
    rrf_k: int = RRF_K,
) -> list[Hit]:
    """Run both searches inside one repo and return the merged best `limit` hits."""
    with translate_db_errors("searching chunks"), pool.connection() as conn:
        by_meaning = vector_search(conn, repo_id, question_vector, per_search)
        by_words = keyword_search(conn, repo_id, question, per_search)
    return reciprocal_rank_fusion([by_meaning, by_words], rrf_k)[:limit]


def vector_search(
    conn: Connection, repo_id: int, question_vector: list[float], limit: int
) -> list[Hit]:
    # With several repos in one table, the HNSW index may find its nearest neighbours in
    # other repos and the repo_id filter would then leave us with too few rows. Iterative
    # scan (pgvector 0.8+) keeps scanning until `limit` matching rows are found.
    conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
    rows = conn.execute(VECTOR_SEARCH_SQL, (repo_id, json.dumps(question_vector), limit))
    return [Hit(*row) for row in rows]


def keyword_search(conn: Connection, repo_id: int, question: str, limit: int) -> list[Hit]:
    query = build_keyword_query(question)
    if query is None:  # the question was only common words: nothing useful to search for
        return []
    rows = conn.execute(KEYWORD_SEARCH_SQL, (repo_id, query, query, limit))
    return [Hit(*row) for row in rows]


def build_keyword_query(question: str) -> str | None:
    """Turn a question into a Postgres tsquery that matches ANY of its meaningful words.

    `plainto_tsquery` would require ALL words, and a natural-language question almost
    never has every word in one chunk. Only letters and digits are kept, so the result
    can never contain tsquery operators and cannot break the query.
    """
    words = re.findall(r"[a-z0-9]+", question.lower())
    meaningful = [w for w in dict.fromkeys(words) if w not in STOP_WORDS and len(w) > 1]
    return " | ".join(meaningful) if meaningful else None


def reciprocal_rank_fusion(rankings: list[list[Hit]], k: int = RRF_K) -> list[Hit]:
    """Merge ranked lists. A chunk scores 1/(k + rank) in each list it appears in (k = 60).

    Cosine distance and ts_rank are on different scales, so their scores cannot be added.
    Ranks can: a chunk that is near the top of both lists beats one that is first in only
    one. Ties keep the order in which chunks were first seen.
    """
    scores: dict[int, float] = {}
    hits: dict[int, Hit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank)
            hits.setdefault(hit.id, hit)
    return [hits[chunk_id] for chunk_id in sorted(scores, key=scores.__getitem__, reverse=True)]
