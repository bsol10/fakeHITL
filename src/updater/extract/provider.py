"""Choosing the model provider from the environment.

``UPDATER_MODEL_PROVIDER`` is ``anthropic`` or ``gemini``, case-insensitive.
Unset or blank means :data:`DEFAULT_PROVIDER`.

The default stays ``anthropic`` until Gemini's live tests have passed against
a real key (decisions log, 2026-10-08). Switching it is one constant, and the
live tests are ``uv run pytest -m live``.

Each provider's module is imported only when it is chosen, so selecting Gemini
never loads ``anthropic`` and selecting Claude never needs the ``gemini`` extra.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from updater.extract.model import EvidenceModel, ModelUnavailable

__all__ = ["DEFAULT_PROVIDER", "PROVIDERS", "PROVIDER_ENV_VAR", "model_from_env"]

PROVIDER_ENV_VAR = "UPDATER_MODEL_PROVIDER"
PROVIDERS = ("anthropic", "gemini")
DEFAULT_PROVIDER = "anthropic"


def model_from_env(environ: Mapping[str, str] | None = None) -> EvidenceModel:
    """The configured provider's model, or :class:`ModelUnavailable` saying why
    there is none. Never a stand-in: a misconfigured run fails here."""
    env = os.environ if environ is None else environ
    provider = env.get(PROVIDER_ENV_VAR, "").strip().lower() or DEFAULT_PROVIDER
    if provider == "anthropic":
        from updater.extract.claude import ClaudeEvidenceModel

        return ClaudeEvidenceModel.from_env(env)
    if provider == "gemini":
        try:
            from updater.extract.gemini import GeminiEvidenceModel
        except ImportError as error:
            raise ModelUnavailable(
                f"the gemini provider needs its SDK ({error.name} is missing). "
                "Run: uv sync --extra gemini"
            ) from error
        return GeminiEvidenceModel.from_env(env)
    raise ModelUnavailable(
        f"{PROVIDER_ENV_VAR}={provider!r} is not a provider; expected one of "
        f"{', '.join(PROVIDERS)}"
    )
