"""Chunker tests: structure-aware Python chunking and the window fallback."""

from collections import Counter

from codelens.ingestion.chunker import (
    MAX_CHUNK_LINES,
    WINDOW_LINES,
    chunk_file,
    split_lines,
)
from codelens.ingestion.walker import walk_repo

PYTHON_SOURCE = '''"""Module docstring."""
import os

LIMIT = 10


def top_level(a):
    return a + LIMIT


class Greeter:
    """Says hello."""

    greeting = "hi"

    def hello(self, name):
        return f"{self.greeting} {name}"

    @staticmethod
    def helper():
        return os.getcwd()


async def fetch():
    return 1
'''


def by_symbol(chunks):
    return {c.symbol: c for c in chunks if c.symbol}


def test_python_chunks_have_expected_kinds_and_symbols():
    chunks = chunk_file("app.py", PYTHON_SOURCE)
    found = {(c.kind, c.symbol) for c in chunks}
    assert found == {
        ("module", None),
        ("function", "top_level"),
        ("class", "Greeter"),
        ("method", "Greeter.hello"),
        ("method", "Greeter.helper"),
        ("function", "fetch"),
    }


def test_chunk_content_matches_its_line_numbers():
    """The citation must point at exactly the text we stored."""
    lines = split_lines(PYTHON_SOURCE)
    for chunk in chunk_file("app.py", PYTHON_SOURCE):
        expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
        assert chunk.content == expected
        assert chunk.content.strip(), "chunks must not be blank"


def test_decorator_is_part_of_the_method_chunk():
    chunk = by_symbol(chunk_file("app.py", PYTHON_SOURCE))["Greeter.helper"]
    assert chunk.content.lstrip().startswith("@staticmethod")


def test_class_chunk_is_only_the_header_not_the_methods():
    chunk = by_symbol(chunk_file("app.py", PYTHON_SOURCE))["Greeter"]
    assert "Says hello." in chunk.content
    assert 'greeting = "hi"' in chunk.content
    assert "def hello" not in chunk.content


def test_module_chunk_holds_imports_and_constants():
    module_chunks = [c for c in chunk_file("app.py", PYTHON_SOURCE) if c.kind == "module"]
    assert len(module_chunks) == 1
    assert module_chunks[0].start_line == 1
    assert "import os" in module_chunks[0].content
    assert "LIMIT = 10" in module_chunks[0].content


def test_citation_format():
    chunk = by_symbol(chunk_file("pkg/app.py", PYTHON_SOURCE))["top_level"]
    assert chunk.citation == f"pkg/app.py:L{chunk.start_line}-L{chunk.end_line}"


def test_nested_class_gets_a_qualified_name():
    source = "class Outer:\n    class Inner:\n        def run(self):\n            return 1\n"
    symbols = {c.symbol for c in chunk_file("n.py", source)}
    assert "Outer.Inner.run" in symbols


def test_class_without_methods_is_one_chunk():
    chunks = chunk_file("c.py", "class Config:\n    a = 1\n    b = 2\n")
    assert [(c.kind, c.symbol) for c in chunks] == [("class", "Config")]


def test_comment_only_runs_between_defs_are_not_chunks():
    source = "def a():\n    return 1\n\n# ---- helpers ----\n\ndef b():\n    return 2\n"
    kinds = [c.kind for c in chunk_file("c.py", source)]
    assert "module" not in kinds


def test_uppercase_py_extension_still_gets_structural_chunking():
    chunks = chunk_file("OLD.PY", "def a():\n    return 1\n")
    assert [(c.kind, c.symbol) for c in chunks] == [("function", "a")]


def test_syntax_error_falls_back_to_windows():
    chunks = chunk_file("broken.py", "def oops(:\n    pass\n")
    assert len(chunks) == 1
    assert chunks[0].kind == "window"


def test_very_long_function_is_split_into_windows_but_keeps_its_symbol():
    body = "\n".join(f"    x{i} = {i}" for i in range(MAX_CHUNK_LINES * 2))
    chunks = chunk_file("big.py", f"def big():\n{body}\n")
    assert len(chunks) > 1
    assert all(c.symbol == "big" and c.kind == "function" for c in chunks)
    assert all(c.end_line - c.start_line + 1 <= WINDOW_LINES for c in chunks)


def test_empty_and_blank_files_produce_no_chunks():
    assert chunk_file("empty.py", "") == []
    assert chunk_file("blank.py", "\n\n   \n") == []
    assert chunk_file("blank.md", "\n\n") == []


def test_non_python_files_use_overlapping_windows():
    text = "\n".join(f"line {i}" for i in range(1, 131))
    chunks = chunk_file("notes.md", text)
    assert all(c.kind == "window" and c.symbol is None for c in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 130
    assert chunks[1].start_line < chunks[0].end_line  # neighbours overlap


def test_line_numbers_survive_windows_line_endings_and_form_feeds():
    source = "def a():\r\n    return 1\r\n\x0c\r\ndef b():\r\n    return 2\r\n"
    chunks = {c.symbol: c for c in chunk_file("w.py", source)}
    assert chunks["b"].start_line == 4
    assert chunks["b"].content == "def b():\n    return 2"


def test_same_content_gives_same_hash():
    first = chunk_file("a.py", "def f():\n    return 1\n")[0]
    second = chunk_file("other.py", "def f():\n    return 1\n")[0]
    assert first.content_hash == second.content_hash


def test_a_file_that_starts_with_a_bom_is_chunked_by_structure_after_reading(tmp_path):
    """End to end: the walker strips the BOM, so the chunker finds the function."""
    (tmp_path / "win.py").write_bytes(b"\xef\xbb\xbfdef f():\n    return 1\n")
    [source] = list(walk_repo(tmp_path, Counter()))

    chunks = chunk_file(source.path, source.text)

    assert [(c.kind, c.symbol) for c in chunks] == [("function", "f")]
