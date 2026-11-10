"""Turn source files into chunks.

Python files are split along their structure (functions, classes, methods) using the
standard library `ast` module. Every other file type, and any Python file that cannot
be parsed, is split into overlapping windows of lines.

Known limitation: inside a class, lines that belong to no method (comments between
methods, attributes declared after the last method) are not emitted as chunks.
"""

import ast
import logging
from collections.abc import Iterator

from codelens.ingestion.models import Chunk, build_chunk

logger = logging.getLogger(__name__)

WINDOW_LINES = 60  # size of a window chunk
WINDOW_OVERLAP = 10  # lines shared between neighbouring windows
MAX_CHUNK_LINES = 120  # structural chunks longer than this are split into windows

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def split_lines(source: str) -> list[str]:
    """Split text into lines the same way Python's parser counts them.

    str.splitlines() also splits on form feeds and similar characters, which would
    make our line numbers disagree with `ast`. So we split on newlines only.
    """
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file ended with a newline
    return lines


def chunk_file(path: str, source: str) -> list[Chunk]:
    """Pick the right chunking strategy for a file."""
    lines = split_lines(source)
    if path.lower().endswith(".py"):
        return chunk_python(path, lines)
    return chunk_windows(path, lines)


# ---- window chunking (generic fallback) ---------------------------------------------


def chunk_windows(path: str, lines: list[str]) -> list[Chunk]:
    """Split a file into overlapping windows of lines. Blank-only windows are skipped."""
    chunks = []
    for start, end in _window_ranges(1, len(lines)):
        if _is_blank(lines, start, end):
            continue
        chunks.append(build_chunk(path, lines, start, end, kind="window"))
    return chunks


def _window_ranges(start: int, end: int) -> Iterator[tuple[int, int]]:
    """Yield (start, end) windows covering start..end, both inclusive."""
    step = WINDOW_LINES - WINDOW_OVERLAP
    window_start = start
    while window_start <= end:
        window_end = min(window_start + WINDOW_LINES - 1, end)
        yield window_start, window_end
        if window_end == end:
            return
        window_start += step


def _is_blank(lines: list[str], start: int, end: int) -> bool:
    return all(not line.strip() for line in lines[start - 1 : end])


# ---- python chunking (structure aware) ------------------------------------------------


def chunk_python(path: str, lines: list[str]) -> list[Chunk]:
    """Split Python source into function, class, method and module-level chunks."""
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError, RecursionError) as exc:
        logger.warning("Cannot parse %s (%s); falling back to line windows.", path, exc)
        return chunk_windows(path, lines)

    # Each entry is (start, end, kind, symbol). Collected first, turned into Chunks below.
    pieces: list[tuple[int, int, str, str | None]] = []
    covered: list[tuple[int, int]] = []  # line ranges taken by top-level defs and classes

    for node in tree.body:
        if isinstance(node, FunctionNode):
            pieces.append((*_span(node), "function", node.name))
            covered.append(_span(node))
        elif isinstance(node, ast.ClassDef):
            pieces.extend(_class_pieces(node, lines, prefix=""))
            covered.append(_span(node))

    # Everything at top level that is not a def or class (imports, constants, scripts).
    pieces.extend(_module_level_pieces(lines, covered))
    pieces.sort(key=lambda piece: piece[0])

    chunks: list[Chunk] = []
    for start, end, kind, symbol in pieces:
        chunks.extend(_chunks_for_piece(path, lines, start, end, kind, symbol))
    return chunks


def _span(node: ast.AST) -> tuple[int, int]:
    """First and last line of a def/class, including its decorators."""
    decorator_lines = [d.lineno for d in getattr(node, "decorator_list", [])]
    return min([node.lineno, *decorator_lines]), node.end_lineno


def _class_pieces(
    node: ast.ClassDef, lines: list[str], prefix: str
) -> list[tuple[int, int, str, str | None]]:
    """A class becomes: one 'class' chunk for its header, plus one chunk per method."""
    name = f"{prefix}{node.name}"
    class_start, class_end = _span(node)

    children = [c for c in node.body if isinstance(c, FunctionNode | ast.ClassDef)]
    if not children:
        return [(class_start, class_end, "class", name)]

    # Header = class line, docstring, attributes: everything before the first child def.
    first_child_start = min(_span(c)[0] for c in children)
    pieces = []
    header = _trim_blank_edges(lines, class_start, first_child_start - 1)
    if header:
        pieces.append((*header, "class", name))

    for child in children:
        if isinstance(child, ast.ClassDef):
            pieces.extend(_class_pieces(child, lines, prefix=f"{name}."))
        else:
            pieces.append((*_span(child), "method", f"{name}.{child.name}"))
    return pieces


def _module_level_pieces(
    lines: list[str], covered: list[tuple[int, int]]
) -> list[tuple[int, int, str, str | None]]:
    """Find runs of top-level lines that are not inside any def or class."""
    is_covered = [False] * (len(lines) + 1)  # index = line number, 1-based
    for start, end in covered:
        for line_no in range(start, end + 1):
            is_covered[line_no] = True

    pieces = []
    run_start = None
    for line_no in range(1, len(lines) + 2):  # +1 so the last run gets closed
        free = line_no <= len(lines) and not is_covered[line_no]
        if free and run_start is None:
            run_start = line_no
        elif not free and run_start is not None:
            trimmed = _trim_blank_edges(lines, run_start, line_no - 1)
            if trimmed and _has_code(lines, *trimmed):
                pieces.append((*trimmed, "module", None))
            run_start = None
    return pieces


def _has_code(lines: list[str], start: int, end: int) -> bool:
    """False if every line is blank or a comment (e.g. a lone '# ---- helpers ----' banner)."""
    return any(line.strip() and not line.strip().startswith("#") for line in lines[start - 1 : end])


def _trim_blank_edges(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    """Shrink a line range so it starts and ends on non-blank lines. None if all blank."""
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    return (start, end) if start <= end else None


def _chunks_for_piece(
    path: str, lines: list[str], start: int, end: int, kind: str, symbol: str | None
) -> list[Chunk]:
    """Build one chunk, or several windows if the piece is too long to embed well."""
    if end - start + 1 <= MAX_CHUNK_LINES:
        return [build_chunk(path, lines, start, end, kind, symbol)]
    return [
        build_chunk(path, lines, window_start, window_end, kind, symbol)
        for window_start, window_end in _window_ranges(start, end)
    ]
