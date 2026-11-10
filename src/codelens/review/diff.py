"""Read a unified diff and reduce it to the files and lines worth reviewing."""

from collections import Counter
from pathlib import PurePosixPath

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError
from unidiff.patch import Hunk

from codelens.errors import InvalidInputError
from codelens.review.models import ChangedFile

# Source code only: reviewing lock files, docs and data produces noise, not findings.
REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".sql", ".sh",
}  # fmt: skip


def parse_diff(text: str, max_added_lines: int) -> tuple[list[ChangedFile], Counter]:
    """Parse `text` and return (files to review, how many files were skipped and why).

    Raises InvalidInputError if the text is not a diff at all.
    """
    try:
        patch = PatchSet(text)
    except UnidiffParseError as exc:
        raise InvalidInputError("The diff could not be parsed. Send a unified diff.") from exc
    if len(patch) == 0:
        raise InvalidInputError("The diff contains no file changes. Send a unified diff.")

    files: list[ChangedFile] = []
    skipped: Counter = Counter()
    for patched_file in patch:
        reason = _skip_reason(patched_file)
        if reason is None:
            changed = _to_changed_file(patched_file)
            reason = _size_or_empty_reason(changed, max_added_lines)
        if reason:
            skipped[reason] += 1
        else:
            files.append(changed)
    return files, skipped


def _skip_reason(patched_file) -> str | None:
    """Why a file is not reviewed, or None if it should be."""
    if patched_file.is_binary_file:
        return "binary"
    if patched_file.is_removed_file:
        return "deleted"  # nothing was added, so there is nothing to comment on
    if PurePosixPath(patched_file.path).suffix.lower() not in REVIEWABLE_EXTENSIONS:
        return "not_code"
    return None


def _size_or_empty_reason(changed: ChangedFile, max_added_lines: int) -> str | None:
    if not changed.added:
        return "no_added_lines"  # a pure deletion: comments can only attach to added lines
    if len(changed.added) > max_added_lines:
        return "too_large"
    return None


def _to_changed_file(patched_file) -> ChangedFile:
    """Collect the added lines, the old-file ranges and a numbered rendering of the hunks."""
    added: dict[int, str] = {}
    old_ranges: list[tuple[int, int]] = []
    rendered: list[str] = []

    for hunk in patched_file:
        old_ranges.append((hunk.source_start, hunk.source_start + max(hunk.source_length, 1) - 1))
        rendered.append(_render_hunk(hunk, added))
    return ChangedFile(
        path=patched_file.path,
        added=added,
        old_ranges=tuple(old_ranges),
        rendered="\n".join(rendered),
    )


def _render_hunk(hunk: Hunk, added: dict[int, str]) -> str:
    """Show a hunk with the NEW file's line numbers, and record its added lines in `added`.

    Models are bad at counting lines in a raw diff, so we print the numbers and let them
    copy one; the reviewer then verifies that the number really is an added line.
    """
    old = f"{hunk.source_start},{hunk.source_length}"
    new = f"{hunk.target_start},{hunk.target_length}"
    lines = [f"@@ -{old} +{new} @@"]
    for line in hunk:
        text = line.value.rstrip("\n")
        if line.is_added:
            added[line.target_line_no] = text
            lines.append(f"{line.target_line_no:>5} + {text}")
        elif line.is_removed:
            lines.append(f"{'':>5} - {text}")
        elif line.is_context:
            lines.append(f"{line.target_line_no:>5}   {text}")
    return "\n".join(lines)
