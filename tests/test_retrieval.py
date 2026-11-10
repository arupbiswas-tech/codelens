"""Retrieval logic that needs no database: query building and rank fusion."""

from codelens.retrieval import Hit, build_keyword_query, reciprocal_rank_fusion


def hit(chunk_id: int) -> Hit:
    return Hit(chunk_id, f"f{chunk_id}.py", None, "function", 1, 2, "code")


def test_keyword_query_ors_the_meaningful_words():
    assert build_keyword_query("Where is login handled?") == "login | handled"


def test_keyword_query_splits_identifiers_like_the_index_does():
    assert build_keyword_query("parse_repo_url") == "parse | repo | url"


def test_keyword_query_drops_duplicates_and_keeps_order():
    assert build_keyword_query("auth auth Auth token") == "auth | token"


def test_keyword_query_is_none_when_only_common_words_remain():
    assert build_keyword_query("Where is the code?") is None
    assert build_keyword_query("???") is None


def test_keyword_query_cannot_contain_tsquery_operators():
    query = build_keyword_query("x'); DROP TABLE chunks; -- | & ! ( ) :*")
    assert query == "drop | table | chunks"  # "x" is too short; punctuation is gone


def test_fusion_puts_chunks_found_by_both_searches_first():
    by_meaning = [hit(1), hit(2), hit(3)]
    by_words = [hit(3), hit(4), hit(1)]

    merged = [h.id for h in reciprocal_rank_fusion([by_meaning, by_words])]

    assert merged[:2] == [1, 3]  # both lists contain them; 1 is higher in the first list
    assert set(merged) == {1, 2, 3, 4}


def test_fusion_lists_each_chunk_once():
    merged = reciprocal_rank_fusion([[hit(1), hit(2)], [hit(2), hit(1)]])
    assert sorted(h.id for h in merged) == [1, 2]


def test_fusion_handles_an_empty_list():
    assert [h.id for h in reciprocal_rank_fusion([[hit(1)], []])] == [1]
    assert reciprocal_rank_fusion([[], []]) == []


def test_agreement_between_both_searches_outweighs_a_single_first_place():
    """RRF's design: two independent searches both finding a chunk is stronger evidence."""
    first_in_one_list_only = hit(1)
    second_in_both = hit(2)
    by_meaning = [first_in_one_list_only, second_in_both]
    by_words = [hit(3), second_in_both]

    merged = reciprocal_rank_fusion([by_meaning, by_words])

    assert merged.index(second_in_both) < merged.index(first_in_one_list_only)
