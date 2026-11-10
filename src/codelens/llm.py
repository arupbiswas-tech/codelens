"""Call a chat model through an OpenAI-compatible API (Groq today, swappable by settings)."""

import json
import logging
import re
from typing import Any

import openai
from openai import OpenAI

from codelens.errors import LLMError

logger = logging.getLogger(__name__)

# Room for the answer and for a reasoning model's hidden "thinking" tokens.
MAX_OUTPUT_TOKENS = 2000


class LLMClient:
    def __init__(self, api_key: str, model: str, base_url: str, timeout: float, max_retries: int):
        self._model = model
        # The SDK already retries 429, 5xx and timeouts with exponential backoff, so we
        # configure it instead of writing our own loop. `timeout` bounds every attempt.
        self._client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )

    def complete(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send one system + user message and return the answer text. Raises LLMError.

        `json_mode=True` asks the provider to return a syntactically valid JSON object.
        That removes a whole class of failures (stray prose, broken quotes); the content
        of the JSON still has to be validated by the caller.

        `reasoning_effort` ("low", "medium" or "high") limits how long a reasoning model
        thinks before answering, which sets both the token bill and the latency. Leave it
        unset for models that do not reason. `max_tokens` replaces the default output cap.
        """
        extra: dict[str, Any] = {"response_format": {"type": "json_object"}} if json_mode else {}
        if reasoning_effort:
            extra["reasoning_effort"] = reasoning_effort
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,  # low: we want faithful answers, not creative ones
                max_completion_tokens=max_tokens or MAX_OUTPUT_TOKENS,
                **extra,
            )
        except openai.APITimeoutError as exc:  # must come before APIConnectionError (its parent)
            raise self._fail("The language model took too long to answer.", exc) from exc
        except openai.BadRequestError as exc:
            if json_mode and "validate JSON" in str(exc):
                # A reasoning model that spends its whole token budget thinking returns
                # nothing, which JSON mode reports as invalid JSON.
                raise self._fail(
                    "The language model did not finish a valid JSON reply (it may have "
                    "run out of output tokens).",
                    exc,
                ) from exc
            raise self._fail("The language model request failed.", exc) from exc
        except openai.RateLimitError as exc:
            raise self._fail("The language model is busy; please retry shortly.", exc) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise self._fail("The language model rejected our credentials.", exc) from exc
        except openai.APIError as exc:  # connection problems and any other API failure
            raise self._fail("The language model request failed.", exc) from exc

        answer = response.choices[0].message.content if response.choices else None
        if not answer or not answer.strip():
            finish_reason = response.choices[0].finish_reason if response.choices else "no choices"
            raise self._fail(f"The language model returned no answer ({finish_reason}).", None)
        return answer.strip()

    @staticmethod
    def _fail(message: str, cause: Exception | None) -> LLMError:
        """Log the real cause for us; the client only sees the short message."""
        logger.error("LLM call failed: %s (%s)", message, cause)
        return LLMError(message)


def parse_json_object(text: str) -> dict | None:
    """Find and parse the JSON object in a model reply. None if there is no valid one.

    Tolerates prose or code fences around the JSON. Never guesses: an unusable reply is
    reported as None so the caller can count it and move on.
    """
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
