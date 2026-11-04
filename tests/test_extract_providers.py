"""The two model providers and the choice between them, with no API key.

Each provider is driven through a scripted fake client that returns the SDK's
own response types, so what is tested is the code that reads a real reply:
which fields the request carries, which model the reply records, and that every
way a call can fail becomes :class:`ModelCallFailed` rather than an empty
result. Configuration errors are :class:`ModelUnavailable`, raised before any
client exists.

The Gemini tests skip when the ``gemini`` extra is not installed; the Claude
tests always run, since ``anthropic`` is a core dependency.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import anthropic
import httpx
import pytest
from anthropic.types import ContentBlock, Message, TextBlock, Usage

from conftest import evidence_reply, finding, make_passage
from updater.extract import (
    ModelCallFailed,
    ModelUnavailable,
    extract_evidence,
    model_from_env,
)
from updater.extract.claude import ClaudeEvidenceModel
from updater.extract.model import MAX_OUTPUT_TOKENS

if TYPE_CHECKING:
    from collections.abc import Callable

SYSTEM = "system prompt"
PROMPT = "user prompt"
SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
REPLY = json.dumps({"evidence": []})

CLAUDE_ENV = {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_MODEL": "claude-opus-5"}
GEMINI_ENV = {
    "UPDATER_MODEL_PROVIDER": "gemini",
    "GEMINI_API_KEY": "gemini-test-key",
    "GEMINI_MODEL": "gemini-test",
}


# Choosing a provider


@pytest.mark.parametrize("setting", [None, "", "  ", "anthropic", "ANTHROPIC"])
def test_anthropic_is_the_default_provider(setting: str | None) -> None:
    env = dict(CLAUDE_ENV)
    if setting is not None:
        env["UPDATER_MODEL_PROVIDER"] = setting

    model = model_from_env(env)

    assert isinstance(model, ClaudeEvidenceModel)
    assert model.model == "claude-opus-5"


def test_gemini_is_chosen_by_name_in_any_case() -> None:
    gemini = pytest.importorskip("updater.extract.gemini")

    model = model_from_env({**GEMINI_ENV, "UPDATER_MODEL_PROVIDER": " Gemini "})

    assert isinstance(model, gemini.GeminiEvidenceModel)
    assert model.model == "gemini-test"


def test_an_unknown_provider_is_refused_with_the_choices() -> None:
    with pytest.raises(ModelUnavailable, match="anthropic, gemini"):
        model_from_env({**CLAUDE_ENV, "UPDATER_MODEL_PROVIDER": "openai"})


def test_choosing_gemini_without_its_sdk_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "updater.extract.gemini", None)

    with pytest.raises(ModelUnavailable, match="uv sync --extra gemini"):
        model_from_env(GEMINI_ENV)


# Claude


@dataclass
class FakeMessages:
    outcome: Message | BaseException
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@dataclass
class FakeAnthropic:
    messages: FakeMessages


def claude_message(
    text: str | None = REPLY,
    *,
    model: str = "claude-opus-5-20260801",
    stop_reason: Any = "end_turn",
) -> Message:
    content: list[ContentBlock] = (
        [] if text is None else [TextBlock(type="text", text=text)]
    )
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def claude(
    outcome: Message | BaseException,
) -> tuple[ClaudeEvidenceModel, FakeMessages]:
    messages = FakeMessages(outcome)
    client = cast("anthropic.Anthropic", FakeAnthropic(messages))
    return ClaudeEvidenceModel(model="claude-opus-5", client=client), messages


def test_claude_needs_a_key() -> None:
    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY"):
        ClaudeEvidenceModel.from_env({"ANTHROPIC_MODEL": "claude-opus-5"})


def test_claude_needs_a_model_id() -> None:
    with pytest.raises(ModelUnavailable, match="ANTHROPIC_MODEL"):
        ClaudeEvidenceModel.from_env({"ANTHROPIC_API_KEY": "sk-ant-test"})


def test_claude_asks_for_json_constrained_by_the_schema() -> None:
    model, messages = claude(claude_message())

    model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)

    [call] = messages.calls
    assert call["model"] == "claude-opus-5"
    assert call["system"] == SYSTEM
    assert call["messages"] == [{"role": "user", "content": PROMPT}]
    assert call["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }
    assert call["max_tokens"] == MAX_OUTPUT_TOKENS


def test_claude_records_the_model_the_api_reports() -> None:
    model, _ = claude(claude_message(model="claude-opus-5-20260801"))

    reply = model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)

    assert reply.model == "claude-opus-5-20260801"
    assert reply.text == REPLY
    assert reply.provider == "anthropic"


_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=_REQUEST),
        anthropic.APITimeoutError(request=_REQUEST),
        anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=_REQUEST), body=None
        ),
        anthropic.InternalServerError(
            "boom", response=httpx.Response(500, request=_REQUEST), body=None
        ),
    ],
    ids=["connection", "timeout", "401", "500"],
)
def test_a_claude_api_failure_is_model_call_failed(error: Exception) -> None:
    model, _ = claude(error)

    with pytest.raises(ModelCallFailed, match="Anthropic API"):
        model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn"])
def test_an_incomplete_claude_reply_is_model_call_failed(stop_reason: str) -> None:
    model, _ = claude(claude_message(stop_reason=stop_reason))

    with pytest.raises(ModelCallFailed, match=stop_reason):
        model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)


def test_a_claude_reply_with_no_text_is_model_call_failed() -> None:
    model, _ = claude(claude_message(text=None))

    with pytest.raises(ModelCallFailed, match="no text"):
        model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)


# Gemini


@pytest.fixture
def gemini() -> Any:
    """The gemini provider module, or a skip when its extra is missing."""
    return pytest.importorskip("updater.extract.gemini")


@dataclass
class FakeModels:
    outcome: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@dataclass
class FakeGenai:
    models: FakeModels


def gemini_response(
    text: str | None = REPLY,
    *,
    model_version: str | None = "gemini-test-001",
    finish_reason: str = "STOP",
    blocked: bool = False,
) -> Any:
    from google.genai import types

    if blocked:
        return types.GenerateContentResponse(
            prompt_feedback=types.GenerateContentResponsePromptFeedback(
                block_reason=types.BlockedReason.SAFETY
            ),
            model_version=model_version,
        )
    parts = [] if text is None else [types.Part(text=text)]
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts),
                finish_reason=types.FinishReason(finish_reason),
            )
        ],
        model_version=model_version,
    )


def gemini_model(gemini: Any, outcome: Any) -> tuple[Any, FakeModels]:
    models = FakeModels(outcome)
    return gemini.GeminiEvidenceModel(
        model="gemini-test", client=FakeGenai(models)
    ), models


def record_client(monkeypatch: pytest.MonkeyPatch, gemini: Any) -> list[dict[str, Any]]:
    """Replace the SDK client constructor, recording what it was given."""
    made: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> object:
        made.append(kwargs)
        return object()

    monkeypatch.setattr(gemini.genai, "Client", fake_client)
    return made


def test_gemini_needs_a_key(gemini: Any) -> None:
    with pytest.raises(ModelUnavailable, match="GEMINI_API_KEY") as raised:
        gemini.GeminiEvidenceModel.from_env({"GEMINI_MODEL": "gemini-test"})

    assert "GOOGLE_API_KEY" in str(raised.value)


def test_gemini_accepts_google_api_key(
    gemini: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group's query.py sets GOOGLE_API_KEY, so that works on its own."""
    made = record_client(monkeypatch, gemini)

    gemini.GeminiEvidenceModel.from_env(
        {"GOOGLE_API_KEY": "from-query-py", "GEMINI_MODEL": "gemini-test"}
    )

    assert made == [{"api_key": "from-query-py", "vertexai": False}]


def test_gemini_api_key_wins_over_google_api_key(
    gemini: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our order, not the SDK's: left alone, the SDK prefers GOOGLE_API_KEY."""
    made = record_client(monkeypatch, gemini)

    gemini.GeminiEvidenceModel.from_env(
        {
            "GEMINI_API_KEY": "the-gemini-one",
            "GOOGLE_API_KEY": "the-google-one",
            "GEMINI_MODEL": "gemini-test",
        }
    )

    assert made[0]["api_key"] == "the-gemini-one"


def test_gemini_needs_a_model_and_has_no_default(gemini: Any) -> None:
    with pytest.raises(ModelUnavailable, match="no default"):
        gemini.GeminiEvidenceModel.from_env({"GEMINI_API_KEY": "gemini-test-key"})


def test_gemini_asks_for_json_constrained_by_the_schema(gemini: Any) -> None:
    model, models = gemini_model(gemini, gemini_response())

    model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)

    [call] = models.calls
    assert call["model"] == "gemini-test"
    assert call["contents"] == PROMPT
    config = call["config"]
    assert config.system_instruction == SYSTEM
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA
    assert config.response_schema is None, "the JSON Schema field, not the other"
    assert config.max_output_tokens == MAX_OUTPUT_TOKENS


def test_gemini_records_the_model_the_api_reports(gemini: Any) -> None:
    model, _ = gemini_model(gemini, gemini_response(model_version="gemini-test-001"))

    reply = model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)

    assert reply.model == "gemini-test-001"
    assert reply.text == REPLY
    assert reply.provider == "gemini"


def _gemini_errors() -> list[tuple[str, Callable[[], Exception]]]:
    def client_error() -> Exception:
        from google.genai import errors

        return errors.ClientError(
            400,
            {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
        )

    def server_error() -> Exception:
        from google.genai import errors

        return errors.ServerError(
            503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}
        )

    return [
        ("400", client_error),
        ("503", server_error),
        ("connection", lambda: httpx.ConnectError("unreachable")),
        ("timeout", lambda: httpx.ReadTimeout("slow")),
    ]


@pytest.mark.parametrize(
    "make_error",
    [make for _, make in _gemini_errors()],
    ids=[name for name, _ in _gemini_errors()],
)
def test_a_gemini_api_failure_is_model_call_failed(
    gemini: Any, make_error: Callable[[], Exception]
) -> None:
    model, _ = gemini_model(gemini, make_error())

    with pytest.raises(ModelCallFailed, match="Gemini API"):
        model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"finish_reason": "MAX_TOKENS"}, "MAX_TOKENS"),
        ({"finish_reason": "SAFETY"}, "SAFETY"),
        ({"blocked": True}, "blocked"),
        ({"text": None}, "no text"),
        ({"model_version": None}, "which model"),
    ],
    ids=["max-tokens", "safety", "prompt-blocked", "no-text", "no-model-version"],
)
def test_an_unusable_gemini_reply_is_model_call_failed(
    gemini: Any, response: dict[str, Any], message: str
) -> None:
    model, _ = gemini_model(gemini, gemini_response(**response))

    with pytest.raises(ModelCallFailed, match=message):
        model.complete(system=SYSTEM, prompt=PROMPT, schema=SCHEMA)


def test_gemini_extraction_end_to_end_keeps_only_verified_quotes(gemini: Any) -> None:
    """The provider wired through the real stage: one quote verifies, one is
    fabricated, and the result records Gemini's reported model."""
    passage = make_passage("Sustained destinations rose to 68% of the cohort.")
    reply = evidence_reply(
        finding(quote="rose to 68%", statistic="68%"),
        finding(quote="rose to 71%", statistic="71%"),
    )
    model, models = gemini_model(gemini, gemini_response(reply))

    result = extract_evidence("Destinations were 64%.", [passage], model=model)

    assert [found.quote for found in result.evidence] == ["rose to 68%"]
    assert result.discarded_count == 1
    assert result.model == "gemini-test-001"
    schema = models.calls[0]["config"].response_json_schema
    assert schema["properties"]["evidence"]["items"]["properties"]["passage_id"][
        "enum"
    ] == [passage.id]
