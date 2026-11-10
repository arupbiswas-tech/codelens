"""Walk a cloned repository and yield the text files worth indexing."""

import logging
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 200_000  # bigger files are usually generated or data, not code

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", "dist", "build",
    ".next", ".idea", ".vscode", "site-packages", "vendor",
}  # fmt: skip

TEXT_EXTENSIONS = {
    ".py", ".md", ".rst", ".txt", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".sql", ".sh", ".yml", ".yaml", ".toml", ".json", ".html", ".css",
}  # fmt: skip

TEXT_FILENAMES = {"Dockerfile", "Makefile"}

SKIP_FILENAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock"}


@dataclass(frozen=True)
class SourceFile:
    path: str  # relative to the repo root, always with forward slashes
    text: str


def walk_repo(root: Path, stats: Counter) -> Iterator[SourceFile]:
    """Yield readable text files under `root`, counting what was skipped in `stats`."""
    for dir_path, dir_names, file_names in os.walk(root):
        # Editing dir_names in place stops os.walk from entering those folders.
        dir_names[:] = sorted(d for d in dir_names if d not in SKIP_DIRS)

        for file_name in sorted(file_names):
            full_path = Path(dir_path) / file_name
            source = _read_if_wanted(full_path, root, stats)
            if source is not None:
                yield source


def _read_if_wanted(full_path: Path, root: Path, stats: Counter) -> SourceFile | None:
    """Return the file if we want to index it, otherwise count why we skipped it."""
    relative = full_path.relative_to(root).as_posix()

    if full_path.is_symlink():
        stats["skipped_symlink"] += 1  # a symlink could point outside the clone
        return None
    if full_path.name in SKIP_FILENAMES or not _is_text_type(full_path):
        stats["skipped_file_type"] += 1
        return None

    try:
        if full_path.stat().st_size > MAX_FILE_BYTES:
            stats["skipped_too_large"] += 1
            return None
        raw = full_path.read_bytes()
    except OSError as exc:
        logger.warning("Cannot read %s: %s", relative, exc)
        stats["skipped_unreadable"] += 1
        return None

    if b"\x00" in raw:
        stats["skipped_binary"] += 1
        return None
    try:
        # "utf-8-sig" also drops a leading byte-order mark, which Windows editors add. Left in,
        # it makes Python's parser fail and the file would silently lose its structure-aware chunks.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        stats["skipped_not_utf8"] += 1
        return None

    stats["files_read"] += 1
    return SourceFile(path=relative, text=text)


def _is_text_type(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in TEXT_FILENAMES
