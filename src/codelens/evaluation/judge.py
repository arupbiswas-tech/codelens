"""An LLM judge that scores an answer for faithfulness and correctness.

Known limitation: by default the judge is the same model that wrote the answer, and models
tend to rate their own style favourably. Treat these numbers as a consistent yardstick for
comparing changes, not as an absolute truth. (Point EVAL_JUDGE_MODEL at another model to
reduce the bias.)
"""

import logging
from dataclasses import dataclass

from codelens.errors import LLMError
from codelens.llm import LLMClient, parse_json_object
from codelens.retrieval import Hit

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """You are a strict reviewer of answers about a code repository.
You get a question, the code snippets the answerer was shown, REFERENCE code that is known \
to contain the true answer, and the answer to review.

Do two things:
1. List every factual claim the answer makes about the code. For each, decide whether the \
SHOWN snippets support it (supported = true) or not (false). Claims about things that appear \
nowhere in the shown snippets are unsupported, even if they sound plausible.
2. Decide whether the answer is correct for the question, judging against the REFERENCE \
code: "yes" (correct and complete enough), "partial" (right idea but missing or wrong in \
important ways) or "no".

Reply with ONLY a JSON object, no other text:
{"claims": [{"claim": "...", "supported": true}], "correct": "yes|partial|no"}"""

VALID_CORRECT = {"yes", "partial", "no"}


@dataclass(frozen=True)
class Verdict:
    claims_total: int
    claims_supported: int
    correct: str  # "yes", "partial" or "no"

    @property
    def faithfulness(self) -> float | None:
        """Share of claims backed by the shown snippets; None if the answer made no claims."""
        return self.claims_supported / self.claims_total if self.claims_total else None


def build_judge_prompt(question: str, answer: str, shown: list[Hit], reference: list[Hit]) -> str:
    """Everything the judge needs in one message."""

    def render(hits: list[Hit]) -> str:
        return "\n\n".join(f"[{h.citation}]\n```\n{h.content}\n```" for h in hits) or "(none)"

    return (
        f"QUESTION:\n{question}\n\n"
        f"SHOWN SNIPPETS:\n{render(shown)}\n\n"
        f"REFERENCE CODE:\n{render(reference)}\n\n"
        f"ANSWER TO REVIEW:\n{answer}"
    )


def parse_verdict(text: str) -> Verdict | None:
    """Read the judge's JSON reply. Returns None if it is unusable (never guesses)."""
    data = parse_json_object(text)
    if data is None:
        return None
    try:
        claims = data["claims"]
        correct = str(data["correct"]).strip().lower()
        supported = sum(1 for claim in claims if claim["supported"] is True)
    except (KeyError, TypeError):
        return None
    if correct not in VALID_CORRECT:
        return None
    return Verdict(claims_total=len(claims), claims_supported=supported, correct=correct)


class Judge:
    def __init__(self, llm: LLMClient):
        self._llm = llm

    def judge(
        self, question: str, answer: str, shown: list[Hit], reference: list[Hit]
    ) -> Verdict | None:
        """Score one answer. Returns None when the judge failed (counted, never hidden)."""
        prompt = build_judge_prompt(question, answer, shown, reference)
        try:
            reply = self._llm.complete(JUDGE_SYSTEM_PROMPT, prompt)
        except LLMError:
            return None
        verdict = parse_verdict(reply)
        if verdict is None:
            logger.warning("Judge reply was not valid JSON: %.200s", reply)
        return verdict
