"""Build eval/review_cases.json: real `git diff`s of small mutations of real repo files.

Run (needs network and git; it is a developer tool, not part of the app):
    docker compose --profile eval run --rm --entrypoint python eval eval/make_review_cases.py

Each case clones one repo at the commit pinned in questions.json, applies ONE textual
mutation, and records the unified diff. A "bug" case injects a defect; a "clean" case is a
harmless refactor that a good reviewer should leave alone. The expected location of a bug is
computed from the diff itself (the added lines), never typed by hand.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from unidiff import PatchSet

HERE = Path(__file__).parent
CONTEXT_LINES = 3  # what GitHub's .diff uses, so the cases look like real pull requests

# (id, repo, kind, description, file, [(old text, new text), ...])
CASES = [
    ("rec-rollback", "kennethreitz/records", "bug",
     "transaction no longer rolls back when the block raises",
     "records.py",
     [("        except:\n            tx.rollback()", "        except:\n            pass")]),
    ("rec-scalar", "kennethreitz/records", "bug",
     "scalar() returns the second column instead of the first",
     "records.py",
     [("return row[0] if row else default", "return row[1] if row else default")]),
    ("rec-sqli", "kennethreitz/records", "bug",
     "query parameters are formatted into the SQL text instead of bound (SQL injection)",
     "records.py",
     [("text(query).bindparams(**params)", "text(query.format(**params))")]),
    ("itd-derive", "pallets/itsdangerous", "bug",
     "key derivation uses the OLDEST secret key instead of the newest (breaks key rotation)",
     "src/itsdangerous/signer.py",
     [("            secret_key = self.secret_keys[-1]\n        else:",
       "            secret_key = self.secret_keys[0]\n        else:")]),
    ("itd-expiry", "pallets/itsdangerous", "bug",
     "the expiry test is inverted: fresh signatures are rejected, old ones accepted",
     "src/itsdangerous/timed.py",
     [("            if age > max_age:", "            if age < max_age:")]),
    ("wpg-days", "arupbiswas-tech/workout-plan-generator", "bug",
     "0 training days is accepted although the message and the prompt need 1 to 7",
     "src/assignment/plan_generator.py",
     [("not 1 <= days_per_week <= 7", "not 0 <= days_per_week <= 7")]),
    ("wpg-clean", "arupbiswas-tech/workout-plan-generator", "clean",
     "a local variable is renamed everywhere it is used",
     "src/assignment/plan_generator.py",
     [("rest_days = 7 - days_per_week", "rest_day_count = 7 - days_per_week"),
      ("{rest_days} rest/recovery", "{rest_day_count} rest/recovery")]),
    ("itd-clean", "pallets/itsdangerous", "clean",
     "a docstring is reworded",
     "src/itsdangerous/encoding.py",
     [("Base64 encode a string of bytes or text. The resulting bytes are\n    safe to use in URLs.",
       "Encode a string of bytes or text as URL-safe base64 without padding. The\n"
       "    result can be used in URLs.")]),
]  # fmt: skip


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def checkout(url: str, commit: str, destination: Path) -> None:
    """Fetch exactly one commit (GitHub allows fetching by SHA) and check it out."""
    destination.mkdir(parents=True)
    git(["init", "-q"], destination)
    git(["fetch", "-q", "--depth", "1", url, commit], destination)
    git(["checkout", "-q", "FETCH_HEAD"], destination)


def make_case(repo: str, info: dict, case: tuple, workdir: Path) -> dict:
    case_id, _repo, kind, description, path, mutations = case
    clone = workdir / case_id
    checkout(info["url"], info["commit"], clone)

    target = clone / path
    text = target.read_text(encoding="utf-8")
    for old, new in mutations:
        if text.count(old) != 1:
            raise SystemExit(f"{case_id}: expected exactly one match for {old!r}, got {text.count(old)}")
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8", newline="")

    diff = git(["diff", f"-U{CONTEXT_LINES}"], clone)
    added = [line.target_line_no for f in PatchSet(diff) for h in f for line in h if line.is_added]
    expected = [{"path": path, "lines": [min(added), max(added)]}] if kind == "bug" else []
    return {
        "id": case_id,
        "repo": repo,
        "kind": kind,
        "description": description,
        "diff": diff,
        "expected": expected,
    }


def main() -> int:
    dataset = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="review-cases-") as tmp:
        cases = [make_case(case[1], dataset["repos"][case[1]], case, Path(tmp)) for case in CASES]

    output = {
        "_about": "Real git diffs of one-line mutations of real files, built by make_review_cases.py. "
        "'bug' cases inject a defect and 'expected' is the added line(s); 'clean' cases are harmless "
        "refactors and must produce no comments.",
        "cases": cases,
    }
    (HERE / "review_cases.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    for case in cases:
        print(f"{case['id']:14} {case['kind']:6} expected={case['expected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
