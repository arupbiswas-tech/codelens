"""Data structures shared by the ingestion modules."""

import hashlib
from dataclasses import dataclass, field


def text_for_model(path: str, symbol: str | None, content: str) -> str:
    """Code plus where it lives. Used to embed chunks and to rerank search hits."""
    header = f"{path}\n{symbol}\n" if symbol else f"{path}\n"
    return header + content


@dataclass(frozen=True)
class Chunk:
    """A piece of a source file that we will embed and search.

    start_line and end_line are 1-based and inclusive, so they can be used directly
    in citations like `src/app/auth.py:L10-L42`.
    """

    path: str
    kind: str  # "function", "class", "method", "module" or "window"
    start_line: int
    end_line: int
    content: str
    content_hash: str
    symbol: str | None = None  # e.g. "Foo.bar"; None for module and window chunks

    @property
    def citation(self) -> str:
        return f"{self.path}:L{self.start_line}-L{self.end_line}"

    @property
    def embedding_text(self) -> str:
        """The text we embed: the code plus where it lives.

        A method body alone does not say which class or file it belongs to. Adding the
        path and symbol lets "where is login handled?" match `auth/service.py` and
        `AuthService.login` even when those words are not in the body. The stored
        `content` (used for citations) stays the raw code.
        """
        return text_for_model(self.path, self.symbol, self.content)


def build_chunk(
    path: str,
    lines: list[str],
    start_line: int,
    end_line: int,
    kind: str,
    symbol: str | None = None,
) -> Chunk:
    """Create a Chunk from a 1-based inclusive line range of `lines`."""
    content = "\n".join(lines[start_line - 1 : end_line])
    return Chunk(
        path=path,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        symbol=symbol,
    )


@dataclass
class IngestResult:
    """Everything produced by ingesting one repository."""

    repo_name: str
    url: str
    commit_sha: str | None
    chunks: list[Chunk] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
