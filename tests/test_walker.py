"""Walker tests: which files get indexed and which are skipped."""

from collections import Counter

from codelens.ingestion.walker import MAX_FILE_BYTES, walk_repo


def write(root, relative, content="x = 1\n", binary=False):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_reads_text_files_with_relative_posix_paths(tmp_path):
    write(tmp_path, "src/app.py")
    write(tmp_path, "README.md", "# hi\n")
    stats = Counter()

    paths = [f.path for f in walk_repo(tmp_path, stats)]

    assert paths == ["README.md", "src/app.py"]
    assert stats["files_read"] == 2


def test_skips_ignored_directories(tmp_path):
    write(tmp_path, "keep.py")
    write(tmp_path, "node_modules/lib/index.js")
    write(tmp_path, ".git/config")
    write(tmp_path, "pkg/__pycache__/mod.py")

    assert [f.path for f in walk_repo(tmp_path, Counter())] == ["keep.py"]


def test_skips_unwanted_types_lock_files_and_binaries(tmp_path):
    write(tmp_path, "image.png", b"\x89PNG\r\n", binary=True)
    write(tmp_path, "uv.lock", "lots of text\n")
    write(tmp_path, "data.py", b"abc\x00def", binary=True)  # .py name but binary content
    stats = Counter()

    assert list(walk_repo(tmp_path, stats)) == []
    assert stats["skipped_file_type"] == 2
    assert stats["skipped_binary"] == 1


def test_skips_large_and_non_utf8_files(tmp_path):
    write(tmp_path, "huge.py", "a" * (MAX_FILE_BYTES + 1))
    write(tmp_path, "latin.py", "caf\xe9 = 1\n".encode("latin-1"), binary=True)
    stats = Counter()

    assert list(walk_repo(tmp_path, stats)) == []
    assert stats["skipped_too_large"] == 1
    assert stats["skipped_not_utf8"] == 1


def test_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    write(outside, "secret.py", "TOKEN = 1\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link.py").symlink_to(outside / "secret.py")
    stats = Counter()

    assert list(walk_repo(repo, stats)) == []
    assert stats["skipped_symlink"] == 1


def test_dockerfile_and_makefile_are_indexed(tmp_path):
    write(tmp_path, "Dockerfile", "FROM python:3.12\n")
    write(tmp_path, "Makefile", "up:\n\techo hi\n")

    assert [f.path for f in walk_repo(tmp_path, Counter())] == ["Dockerfile", "Makefile"]


def test_a_byte_order_mark_is_removed_so_python_files_still_parse(tmp_path):
    write(tmp_path, "bom.py", b"\xef\xbb\xbfdef f():\n    return 1\n", binary=True)

    [source] = list(walk_repo(tmp_path, Counter()))

    assert source.text == "def f():\n    return 1\n"
