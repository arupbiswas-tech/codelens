"""Diff parsing tests: which files and lines the reviewer gets to see."""

import pytest

from codelens.errors import InvalidInputError
from codelens.review.diff import parse_diff

PY_DIFF = """diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -10,5 +10,6 @@ def f(x):
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
 
 print(1)
"""


def only_file(text, max_added=300):
    files, skipped = parse_diff(text, max_added)
    assert len(files) == 1, skipped
    return files[0]


def test_added_lines_are_keyed_by_their_new_file_line_number():
    changed = only_file(PY_DIFF)

    assert changed.path == "src/a.py"
    assert changed.added == {11: "    b = 3", 12: "    c = 4"}


def test_the_rendering_shows_new_line_numbers_and_marks_additions_and_removals():
    rendered = only_file(PY_DIFF).rendered.splitlines()

    assert rendered[0] == "@@ -10,5 +10,6 @@"
    assert "   10       a = 1" in rendered  # unchanged line keeps its new number
    assert "      -     b = 2" in rendered  # removed lines have no number
    assert "   11 +     b = 3" in rendered
    assert "   12 +     c = 4" in rendered


def test_old_ranges_cover_the_lines_the_hunk_touches_in_the_old_file():
    assert only_file(PY_DIFF).old_ranges == ((10, 14),)


def test_several_hunks_are_all_kept():
    text = PY_DIFF + """@@ -40,3 +41,4 @@ def g():
     x = 1
+    y = 2
     return x
 end
"""

    changed = only_file(text)

    assert set(changed.added) == {11, 12, 42}
    assert len(changed.old_ranges) == 2


@pytest.mark.parametrize(
    ("path", "reason"),
    [("README.md", "not_code"), ("uv.lock", "not_code"), ("data.json", "not_code")],
)
def test_non_code_files_are_skipped(path, reason):
    text = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1,1 +1,2 @@
 old
+new
"""
    files, skipped = parse_diff(text, 300)

    assert files == [] and skipped[reason] == 1


def test_deleted_files_and_pure_deletions_are_skipped_because_nothing_can_be_commented():
    deleted = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
"""
    only_removed_lines = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,3 +1,2 @@
 keep
-drop
 keep2
"""
    assert parse_diff(deleted, 300)[1]["deleted"] == 1
    assert parse_diff(only_removed_lines, 300)[1]["no_added_lines"] == 1


def test_a_file_with_too_many_added_lines_is_skipped():
    body = "".join(f"+line {i}\n" for i in range(5))
    text = f"""diff --git a/big.py b/big.py
--- a/big.py
+++ b/big.py
@@ -1,1 +1,6 @@
 keep
{body}"""

    files, skipped = parse_diff(text, max_added_lines=3)

    assert files == [] and skipped["too_large"] == 1


def test_a_new_file_is_reviewed_with_all_lines_added():
    text = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
"""
    assert only_file(text).added == {1: "a = 1", 2: "b = 2"}


def test_a_renamed_and_edited_file_uses_its_new_path():
    text = """diff --git a/old_name.py b/new_name.py
similarity index 80%
rename from old_name.py
rename to new_name.py
--- a/old_name.py
+++ b/new_name.py
@@ -1,2 +1,2 @@
 keep
-old
+new
"""
    assert only_file(text).path == "new_name.py"


def test_the_no_newline_marker_is_ignored():
    marker = "\\" + " No newline at end of file"  # a backslash, then the git marker text
    newline = chr(10)
    text = newline.join(
        [
            "diff --git a/a.py b/a.py",
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1,1 +1,1 @@",
            "-x = 1",
            marker,
            "+x = 2",
            marker,
            "",
        ]
    )

    changed = only_file(text)

    assert changed.added == {1: "x = 2"}
    assert "No newline" not in changed.rendered


@pytest.mark.parametrize("text", ["", "   \n", "just some text", "hello\nworld"])
def test_text_that_is_not_a_diff_is_a_clean_error(text):
    with pytest.raises(InvalidInputError):
        parse_diff(text, 300)


def test_mixed_diffs_keep_code_and_report_what_was_skipped():
    text = PY_DIFF + """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 hi
+there
"""
    files, skipped = parse_diff(text, 300)

    assert [f.path for f in files] == ["src/a.py"]
    assert skipped == {"not_code": 1}
