"""Reading the model's JSON, and the mechanical (no LLM) checks on its comments."""

import logging

from codelens.answering import extract_citations
from codelens.llm import parse_json_object
from codelens.retrieval import Hit
from codelens.review.models import ChangedFile, Comment, DroppedComment

logger = logging.getLogger(__name__)

SEVERITIES = ("high", "medium", "low")
SEVERITY_ORDER = {name: rank for rank, name in enumerate(SEVERITIES)}


def parse_comments(reply: str, path: str, max_comments: int) -> list[Comment] | None:
    """Turn the review reply into Comment objects. None if the reply is unusable.

    Entries that are individually malformed are skipped (and logged); a reply with no
    "comments" list at all is reported as None so the caller can ask again.
    """
    data = parse_json_object(reply)
    if data is None or not isinstance(data.get("comments"), list):
        return None

    comments = []
    for entry in data["comments"]:
        if len(comments) == max_comments:
            break  # the cap counts valid comments, so a malformed entry cannot use it up
        comment = _to_comment(entry, path)
        if comment is None:
            logger.warning("Skipping a malformed review comment: %.200s", entry)
        else:
            comments.append(comment)
    return comments


def _to_comment(entry: object, path: str) -> Comment | None:
    if not isinstance(entry, dict):
        return None
    try:
        line = int(entry["line"])
    except (KeyError, TypeError, ValueError):
        return None
    body = entry.get("body")
    if not isinstance(body, str) or not body.strip():
        return None

    severity = str(entry.get("severity", "")).strip().lower()
    evidence = entry.get("evidence")
    return Comment(
        path=path,
        line=line,
        severity=severity if severity in SEVERITIES else "medium",
        body=body.strip(),
        evidence=tuple(str(e) for e in evidence) if isinstance(evidence, list) else (),
    )


def anchor_and_dedupe(
    comments: list[Comment], file: ChangedFile
) -> tuple[list[Comment], list[DroppedComment]]:
    """Keep only comments that sit on a really-added line, and drop repeats.

    This is the cheap, certain half of the groundedness check: whatever line number the
    model claims, the comment can only be shown if that line exists in the diff as an
    addition. (Models miscount lines constantly; this makes that harmless.)
    """
    kept: list[Comment] = []
    dropped: list[DroppedComment] = []
    seen: set[tuple[int, str]] = set()
    for comment in comments:
        if comment.line not in file.added:
            detail = f"line {comment.line} is not an added line of {file.path}"
            dropped.append(DroppedComment(comment, "not_an_added_line", detail))
        elif (comment.line, comment.body) in seen:
            dropped.append(DroppedComment(comment, "duplicate"))
        else:
            seen.add((comment.line, comment.body))
            kept.append(comment)
    return kept, dropped


def verify_evidence(comment: Comment, context: list[Hit]) -> Comment:
    """Keep only the evidence references that point at code the model really was shown.

    A reference to code that was never retrieved is a made-up citation: it is removed, so
    the response never claims support it does not have.
    """
    if not comment.evidence:
        return comment
    verified, _made_up = extract_citations(" ".join(comment.evidence), context)
    return Comment(
        comment.path,
        comment.line,
        comment.severity,
        comment.body,
        evidence=tuple(hit.citation for hit in verified),
        checked=comment.checked,
    )


VERDICTS = ("supported", "partly", "unsupported")


def parse_verdicts(reply: str, count: int) -> dict[int, tuple[str, str]] | None:
    """Read the check reply: {comment id: (verdict, reason)}. None if unusable.

    A verdict is "supported", "partly" or "unsupported". A model that answers with the
    older `"supported": true/false` form is understood too. Entries that make no sense are
    skipped, and a comment without a verdict is treated as unsupported by the caller.
    """
    data = parse_json_object(reply)
    if data is None or not isinstance(data.get("verdicts"), list):
        return None

    verdicts: dict[int, tuple[str, str]] = {}
    for entry in data["verdicts"]:
        try:
            comment_id = int(entry["id"])
            verdict = _read_verdict(entry)
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= comment_id < count:
            verdicts[comment_id] = (verdict, str(entry.get("reason", "")))
    return verdicts


def _read_verdict(entry: dict) -> str:
    word = str(entry.get("verdict", "")).strip().lower()
    if word in VERDICTS:
        return word
    if "verdict" not in entry:
        return "supported" if entry["supported"] is True else "unsupported"
    return "unsupported"  # an unknown word is not a vouch
