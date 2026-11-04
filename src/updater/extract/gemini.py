"""Gemini behind :class:`~updater.extract.model.EvidenceModel`.

The only module that imports ``google-genai`` (``uv sync --extra gemini``). It
uses the SDK directly, not LlamaIndex as the group's prototype in
``src/backend`` does: structured output is one config field, and the stage
needs nothing an agent framework adds (BUILD_SPEC.md section 7).

Structured output is ``response_json_schema``, which requires
``response_mime_type="application/json"``. The SDK's docstring for it lists the
keywords Gemini supports, and
:func:`updater.extract.evidence.evidence_schema` uses only those.

Configuration, from the environment (``.env`` via the CLI):

``GEMINI_API_KEY``, else ``GOOGLE_API_KEY``
    One is required. ``GOOGLE_API_KEY`` is accepted because the group's
    ``query.py`` sets it. The key is passed explicitly because the SDK on its
    own prefers ``GOOGLE_API_KEY``, the opposite order.
``GEMINI_MODEL``
    Required, with no default: a default nobody has verified would fail at the
    first call instead of at configuration time. The live tests check that the
    configured model is in the API's model list.

The client is pinned to ``vertexai=False``, or ``GOOGLE_GENAI_USE_VERTEXAI`` in
the environment would silently move it to Vertex AI, where an API key does not
work.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from updater.extract.model import (
    MAX_OUTPUT_TOKENS,
    ModelCallFailed,
    ModelReply,
    ModelUnavailable,
)

__all__ = ["API_KEY_ENV_VARS", "MODEL_ENV_VAR", "GeminiEvidenceModel"]

API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
"""Read in this order; the first one set is used."""
MODEL_ENV_VAR = "GEMINI_MODEL"


class GeminiEvidenceModel:
    """Answers extraction prompts with Gemini, constrained to a JSON schema."""

    provider = "gemini"

    def __init__(self, *, model: str, client: genai.Client) -> None:
        self.model = model
        self._client = client

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GeminiEvidenceModel:
        env = os.environ if environ is None else environ
        key = next(
            (
                env[name].strip()
                for name in API_KEY_ENV_VARS
                if env.get(name, "").strip()
            ),
            None,
        )
        if key is None:
            raise ModelUnavailable(
                f"{API_KEY_ENV_VARS[0]} is not set (and neither is "
                f"{API_KEY_ENV_VARS[1]}). Add it to .env (see .env.example) to use "
                "the gemini provider."
            )
        model = env.get(MODEL_ENV_VAR, "").strip()
        if not model:
            raise ModelUnavailable(
                f"{MODEL_ENV_VAR} is not set, and there is deliberately no default: "
                "choose a model your key can call and put it in .env."
            )
        return cls(model=model, client=genai.Client(api_key=key, vertexai=False))

    def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any]
    ) -> ModelReply:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=dict(schema),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=prompt, config=config
            )
        except (
            errors.APIError,
            errors.UnknownApiResponseError,
            httpx.HTTPError,
        ) as error:
            raise ModelCallFailed(
                f"the Gemini API call to {self.model} failed: {error}"
            ) from error

        answered_by = response.model_version
        if not answered_by:
            raise ModelCallFailed(
                f"the reply to a call for {self.model} did not say which model "
                "produced it"
            )
        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason is not None:
            raise ModelCallFailed(
                f"{answered_by} blocked the prompt: {feedback.block_reason}"
            )
        candidate = response.candidates[0] if response.candidates else None
        finish = None if candidate is None else candidate.finish_reason
        if finish is not None and finish != types.FinishReason.STOP:
            raise ModelCallFailed(
                f"{answered_by} stopped with {finish}, so its reply is incomplete"
            )
        text = response.text
        if not text or not text.strip():
            raise ModelCallFailed(f"{answered_by} returned no text")
        return ModelReply(text=text, model=answered_by, provider=self.provider)
