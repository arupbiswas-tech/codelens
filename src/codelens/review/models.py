"""Data structures shared by the review modules."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChangedFile:
    """One file of a pull request, reduced to what the reviewer needs."""

    path: str
    added: dict[int, str]  # new-file line number -> text, for every ADDED line
    old_ranges: tuple[tuple[int, int], ...]  # line ranges of the OLD file touched by the hunks
    rendered: str  # the hunks with explicit new-file line numbers, as shown to the model


@dataclass(frozen=True)
class Comment:
    """One review comment, anchored to an added line of the diff."""

    path: str
    line: int  # a line number in the NEW version of the file
    severity: str  # "high", "medium" or "low"
    body: str
    evidence: tuple[str, ...] = ()  # verified `path:L1-L9` references to related code
    checked: bool = True  # False when the self-check could not run and the comment is unverified
    note: str = ""  # the checker's caveat when the main problem is real but a side remark is not


@dataclass(frozen=True)
class DroppedComment:
    """A comment the reviewer wrote but the checks rejected, kept for transparency."""

    comment: Comment
    reason: str  # "not_an_added_line", "duplicate" or "unsupported"
    detail: str = ""


@dataclass
class ReviewResult:
    comments: list[Comment] = field(default_factory=list)
    dropped: list[DroppedComment] = field(default_factory=list)
    files_reviewed: int = 0
    files_skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    errors: list[str] = field(default_factory=list)  # per-file problems, safe to show
    partial: bool = False  # True if a limit (file cap or deadline) stopped the review early
