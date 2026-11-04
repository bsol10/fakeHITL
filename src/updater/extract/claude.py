"""Claude behind :class:`~updater.extract.model.EvidenceModel`.

The only module that imports ``anthropic``, and only
:func:`updater.extract.provider.model_from_env` imports it, when the anthropic
provider is selected.

Structured output is the Messages API's ``output_config`` with a
``json_schema`` format (``anthropic.types.OutputConfigParam``). No sampling
parameters are sent, so a model change cannot be refused over a parameter the
new model no longer accepts.

``ANTHROPIC_API_KEY`` and ``ANTHROPIC_MODEL`` are both required, from the
environment (``.env`` via the CLI). The model id has no default in code: it is
configuration, and ``.env.example`` carries the project's choice.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import anthropic

from updater.extract.model import (
    MAX_OUTPUT_TOKENS,
    ModelCallFailed,
    ModelReply,
    ModelUnavailable,
)

__all__ = ["API_KEY_ENV_VAR", "MODEL_ENV_VAR", "ClaudeEvidenceModel"]

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
MODEL_ENV_VAR = "ANTHROPIC_MODEL"

_COMPLETE = frozenset({"end_turn", "stop_sequence"})
"""Stop reasons after which the reply is whole. Anything else — ``max_tokens``,
``refusal``, ``pause_turn`` — leaves JSON that is cut off or absent."""


class ClaudeEvidenceModel:
    """Answers extraction prompts with Claude, constrained to a JSON schema."""

    provider = "anthropic"

    def __init__(self, *, model: str, client: anthropic.Anthropic) -> None:
        self.model = model
        self._client = client

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ClaudeEvidenceModel:
        env = os.environ if environ is None else environ
        key = env.get(API_KEY_ENV_VAR, "").strip()
        if not key:
            raise ModelUnavailable(
                f"{API_KEY_ENV_VAR} is not set. Add it to .env (see .env.example) "
                "to use the anthropic provider."
            )
        model = env.get(MODEL_ENV_VAR, "").strip()
        if not model:
            raise ModelUnavailable(
                f"{MODEL_ENV_VAR} is not set. The model id is configuration, not "
                "code: copy the value from .env.example into .env."
            )
        return cls(model=model, client=anthropic.Anthropic(api_key=key))

    def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any]
    ) -> ModelReply:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": dict(schema)}
                },
            )
        except anthropic.APIError as error:
            raise ModelCallFailed(
                f"the Anthropic API call to {self.model} failed: {error}"
            ) from error

        if message.stop_reason not in _COMPLETE:
            raise ModelCallFailed(
                f"{message.model} stopped with {message.stop_reason!r}, so its "
                "reply is incomplete"
            )
        text = "".join(
            block.text
            for block in message.content
            if isinstance(block, anthropic.types.TextBlock)
        )
        if not text.strip():
            raise ModelCallFailed(f"{message.model} returned no text")
        return ModelReply(text=text, model=message.model, provider=self.provider)
