"""Load and validate the hand-written evaluation set (eval/questions.json)."""

import json
from dataclasses import dataclass
from pathlib import Path


class DatasetError(ValueError):
    """The evaluation file is malformed. The message says exactly what to fix."""


@dataclass(frozen=True)
class Label:
    """Where the answer lives: a symbol (stable when lines move) or a line range."""

    path: str
    symbol: str | None = None
    lines: tuple[int, int] | None = None

    def __str__(self) -> str:
        return f"{self.path}:{self.symbol}" if self.symbol else f"{self.path}:L{self.lines}"


@dataclass(frozen=True)
class Question:
    id: str
    repo: str
    question: str
    category: str
    answerable: bool
    expected: tuple[Label, ...]  # alternatives: finding any one of them counts as a hit


@dataclass(frozen=True)
class RepoInfo:
    url: str
    commit: str  # the commit the labels were written against


@dataclass(frozen=True)
class Dataset:
    repos: dict[str, RepoInfo]
    questions: list[Question]

    def answerable(self) -> list[Question]:
        return [q for q in self.questions if q.answerable]


def load_dataset(path: Path) -> Dataset:
    """Read the JSON file and check it thoroughly, so a typo fails here and not mid-run."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Cannot read {path}: {exc}") from exc

    repos = {name: _parse_repo(name, info) for name, info in raw.get("repos", {}).items()}
    if not repos:
        raise DatasetError("The dataset lists no repos.")

    questions = [_parse_question(item, repos) for item in raw.get("questions", [])]
    ids = [q.id for q in questions]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise DatasetError(f"Duplicate question ids: {sorted(duplicates)}")
    if not questions:
        raise DatasetError("The dataset has no questions.")
    return Dataset(repos=repos, questions=questions)


def _parse_repo(name: str, info: dict) -> RepoInfo:
    for key in ("url", "commit"):
        if not info.get(key):
            raise DatasetError(f"Repo '{name}' is missing '{key}'.")
    return RepoInfo(url=info["url"], commit=info["commit"])


def _parse_question(item: dict, repos: dict[str, RepoInfo]) -> Question:
    qid = item.get("id", "<no id>")
    for key in ("id", "repo", "question", "category"):
        if not item.get(key):
            raise DatasetError(f"Question {qid}: missing '{key}'.")
    if item["repo"] not in repos:
        raise DatasetError(f"Question {qid}: repo '{item['repo']}' is not listed under 'repos'.")

    answerable = item.get("answerable", True)
    labels = tuple(_parse_label(qid, label) for label in item.get("expected", []))
    if answerable and not labels:
        raise DatasetError(f"Question {qid}: an answerable question needs at least one label.")
    if not answerable and labels:
        raise DatasetError(f"Question {qid}: an unanswerable question must have no labels.")
    return Question(qid, item["repo"], item["question"], item["category"], answerable, labels)


def _parse_label(qid: str, label: dict) -> Label:
    path, symbol, lines = label.get("path"), label.get("symbol"), label.get("lines")
    if not path:
        raise DatasetError(f"Question {qid}: a label has no 'path'.")
    if (symbol is None) == (lines is None):
        raise DatasetError(f"Question {qid}: label for {path} needs exactly one of symbol/lines.")
    if lines is not None:
        valid = len(lines) == 2 and 1 <= lines[0] <= lines[1]
        if not valid:
            raise DatasetError(f"Question {qid}: bad line range {lines} for {path}.")
        return Label(path=path, lines=(lines[0], lines[1]))
    return Label(path=path, symbol=symbol)
