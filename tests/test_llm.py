"""LLM client tests: every failure kind becomes a short LLMError, never a raw SDK error."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from codelens.errors import LLMError
from codelens.llm import LLMClient

REQUEST = httpx.Request("POST", "http://llm.test/v1/chat/completions")


def status_error(error_class, status, message="secret provider detail"):
    response = httpx.Response(status, request=REQUEST)
    return error_class(message, response=response, body=None)


class FakeCompletions:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raises:
            raise self.raises
        return self.result


def reply(content, finish_reason="stop"):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


def make_client(completions: FakeCompletions) -> LLMClient:
    client = LLMClient("key", "some-model", "http://llm.test/v1", timeout=5, max_retries=0)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client


def test_returns_the_trimmed_answer_and_sends_both_messages():
    completions = FakeCompletions(reply("  hello  "))

    assert make_client(completions).complete("be brief", "question") == "hello"

    sent = completions.last_kwargs
    assert sent["model"] == "some-model"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=REQUEST), "too long"),
        (status_error(openai.RateLimitError, 429), "busy"),
        (status_error(openai.AuthenticationError, 401), "credentials"),
        (status_error(openai.PermissionDeniedError, 403), "credentials"),
        (status_error(openai.InternalServerError, 500), "request failed"),
        (openai.APIConnectionError(request=REQUEST), "request failed"),
    ],
)
def test_sdk_errors_become_llm_errors_without_provider_details(error, expected):
    with pytest.raises(LLMError, match=expected) as excinfo:
        make_client(FakeCompletions(raises=error)).complete("s", "u")

    assert "secret provider detail" not in excinfo.value.message


@pytest.mark.parametrize("content", [None, "", "   "])
def test_an_empty_answer_is_an_error_that_names_the_finish_reason(content):
    completions = FakeCompletions(reply(content, finish_reason="length"))

    with pytest.raises(LLMError, match="no answer.*length"):
        make_client(completions).complete("s", "u")


def test_a_response_with_no_choices_is_an_error():
    with pytest.raises(LLMError, match="no answer"):
        make_client(FakeCompletions(SimpleNamespace(choices=[]))).complete("s", "u")


def test_json_mode_asks_the_provider_for_a_json_object_and_is_off_by_default():
    plain = FakeCompletions(reply("hi"))
    make_client(plain).complete("s", "u")
    assert "response_format" not in plain.last_kwargs

    json_call = FakeCompletions(reply("{}"))
    make_client(json_call).complete("s", "u", json_mode=True)
    assert json_call.last_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('Sure!\n```json\n{"a": {"b": 2}}\n```', {"a": {"b": 2}}),
        ("no json", None),
        ("{broken", None),
        ("[1, 2, 3]", None),  # valid JSON, but not an object
        ("", None),
    ],
)
def test_parse_json_object_finds_an_object_or_returns_none(text, expected):
    from codelens.llm import parse_json_object

    assert parse_json_object(text) == expected


def test_reasoning_effort_and_the_output_cap_are_passed_only_when_given():
    plain = FakeCompletions(reply("hi"))
    make_client(plain).complete("s", "u")
    assert "reasoning_effort" not in plain.last_kwargs
    assert plain.last_kwargs["max_completion_tokens"] == 2000

    tuned = FakeCompletions(reply("hi"))
    make_client(tuned).complete("s", "u", reasoning_effort="low", max_tokens=4000)
    assert tuned.last_kwargs["reasoning_effort"] == "low"
    assert tuned.last_kwargs["max_completion_tokens"] == 4000


def test_a_json_validation_failure_is_reported_as_running_out_of_tokens():
    error = status_error(
        openai.BadRequestError, 400, "Failed to validate JSON. Please adjust your prompt."
    )

    with pytest.raises(LLMError, match="run out of output tokens"):
        make_client(FakeCompletions(raises=error)).complete("s", "u", json_mode=True)


def test_other_bad_requests_stay_a_generic_failure():
    error = status_error(openai.BadRequestError, 400)

    with pytest.raises(LLMError, match="request failed"):
        make_client(FakeCompletions(raises=error)).complete("s", "u", json_mode=True)
