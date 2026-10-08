"""The provider interface: what evidence extraction needs from a language model.

BACKEND_PLAN.md phase 5 puts the model "behind a small provider interface", and
this is all of it: send a system prompt, a user prompt and a JSON schema, get
back the JSON the model wrote and the name of the model that wrote it. Nothing
here knows about passages, anchors or claims — :mod:`updater.extract.evidence`
owns those — and nothing here imports a provider SDK, so the interface can be
imported, implemented and faked without either SDK installed.

Two failures, kept apart because they need different people:

:class:`ModelUnavailable`
    No model can be called at all: a key or a model id is missing, the SDK is
    not installed, the provider name is wrong. A configuration error, raised
    before any request is sent. It is never turned into an empty result — an
    empty result reads as "the sources say nothing", which is a finding about
    the world, and a missing key is not one.
:class:`ModelCallFailed`
    The provider was called and did not return a usable reply: an API or
    network error, a refusal, a reply cut off at the token limit, a reply with
    no text. Also never an empty result, for the same reason.

The model id is configuration, not code (BACKEND_PLAN.md phase 5): each
provider reads it from the environment and refuses to start without one.
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
    """The provider was called and returned nothing usable."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReply:
    """What came back from one call.

    ``model`` is the model the API *says* answered, not the one that was asked
    for. The two differ whenever the configured id is an alias, and a reviewer
    asking "which model wrote this?" a year later needs the one that did.
    """

    text: str
    """The reply's JSON exactly as the model wrote it. Parsing and checking it
    is the extract stage's job, not the provider's."""
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
