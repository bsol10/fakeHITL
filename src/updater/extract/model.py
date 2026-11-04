"""The provider interface: what evidence extraction needs from a language model.

Send a system prompt, a user prompt and a JSON schema; get back the JSON the
model wrote and the name of the model that wrote it (BACKEND_PLAN.md phase 5).
Nothing here knows about passages or claims, and nothing imports a provider
SDK, so the interface can be implemented and faked without either installed.

Neither error is ever turned into an empty result: an empty result reads as
"the sources say nothing", a finding about the world, and a missing key or a
failed call is not one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "EvidenceModel",
    "ModelCallFailed",
    "ModelError",
    "ModelReply",
    "ModelUnavailable",
]

MAX_OUTPUT_TOKENS = 16_000
"""Ceiling on one reply, the same for every provider. A reply that reaches it is
cut off mid-JSON and is refused as :class:`ModelCallFailed`, never repaired."""


class ModelError(Exception):
    """Base for every way a model can fail to answer."""


class ModelUnavailable(ModelError):
    """No model can be called: missing key, missing model id, missing SDK, or an
    unknown provider. Raised before any request is sent."""


class ModelCallFailed(ModelError):
    """The provider was called and returned nothing usable: an API or network
    error, a refusal, a reply cut off at the token limit, no text, or JSON that
    is not the requested shape."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReply:
    """What came back from one call.

    ``model`` is the model the API *says* answered, not the one asked for; the
    two differ whenever the configured id is an alias.
    """

    text: str
    """The reply's JSON exactly as the model wrote it, unparsed."""
    model: str
    provider: str


class EvidenceModel(Protocol):
    """Anything that can answer one prompt with JSON constrained by a schema."""

    provider: str
    model: str
    """The configured model id — what is asked for. :attr:`ModelReply.model`
    is what answered."""

    def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any]
    ) -> ModelReply: ...
