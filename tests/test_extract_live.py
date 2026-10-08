"""Evidence extraction against the real model APIs. Paid, so never by default.

Marked ``live`` and deselected by ``addopts``; run them with::

    uv run pytest -m live

Every test runs once per provider. A provider whose key is not set (in the
environment or in ``.env``) is skipped; a provider whose key *is* set but whose
model id is not fails, with the message saying what to set, because that is a
configuration mistake rather than a missing provider. ``.env`` is read without
being loaded into the process environment, so collecting this module changes
nothing for the rest of the suite.

These are the tests that say whether a provider can be trusted with the
pipeline, and the decisions log (2026-10-08) makes Gemini the default only
once its run of them passes. The property that matters most is the second:
passages that state no figure must come back with ``statistic`` null.
"""

from __future__ import annotations

import os

import pytest
from dotenv import dotenv_values, find_dotenv

from conftest import make_passage
from updater.extract import (
    PROVIDER_ENV_VAR,
    EvidenceModel,
    extract_evidence,
    model_from_env,
)
from updater.models import BlockLocator

pytestmark = pytest.mark.live

KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

DESTINATIONS = make_passage(
    "Sustained destinations rose to 68% of the 2024 leaver cohort, measured six "
    "months after the end of key stage 4. This continues the upward trend "
    "reported in each of the previous three publications.",
    passage_id="dfe-destinations-2025:page7",
)
METHOD = make_passage(
    "Figures are drawn from matched administrative records covering all "
    "state-funded schools in England. Coverage of independent training "
    "providers remains incomplete.",
    passage_id="dfe-destinations-2025:para4",
    locator=BlockLocator(paragraph_index=4, style=""),
)


def _environment() -> dict[str, str]:
    """``.env`` under the real environment: a variable set in the shell wins."""
    from_file = dotenv_values(find_dotenv(usecwd=True))
    merged = {key: value for key, value in from_file.items() if value is not None}
    merged.update(os.environ)
    return merged


@pytest.fixture(params=sorted(KEYS))
def live_env(request: pytest.FixtureRequest) -> dict[str, str]:
    provider: str = request.param
    env = _environment()
    if not any(env.get(name, "").strip() for name in KEYS[provider]):
        pytest.skip(f"{KEYS[provider][0]} is not set")
    if provider == "gemini":
        pytest.importorskip("google.genai")
    return {**env, PROVIDER_ENV_VAR: provider}


@pytest.fixture
def live_model(live_env: dict[str, str]) -> EvidenceModel:
    return model_from_env(live_env)


def test_a_stated_figure_is_found_quoted_and_verified(
    live_model: EvidenceModel,
) -> None:
    result = extract_evidence(
        "Sustained destinations were 64% of the leaver cohort.",
        [DESTINATIONS, METHOD],
        model=live_model,
    )

    assert result.evidence, f"nothing kept; discarded: {result.discarded}"
    figures = [found for found in result.evidence if found.evidence.statistic]
    assert any("68" in (found.evidence.statistic or "") for found in figures)
    for found in result.evidence:
        assert found.quote in found.passage.text


def test_a_passage_with_no_figure_yields_no_statistic(
    live_model: EvidenceModel,
) -> None:
    """The most important test in the project (PROJECT_OVERVIEW.md section 15):
    a model that must emit a number will invent one."""
    result = extract_evidence(
        "The destination figures come from administrative records.",
        [METHOD],
        model=live_model,
    )

    assert result.evidence, f"nothing kept; discarded: {result.discarded}"
    for found in result.evidence:
        assert found.evidence.statistic is None, found.evidence


def test_passages_that_say_nothing_about_the_claim_yield_no_evidence(
    live_model: EvidenceModel,
) -> None:
    """Abstention: the claim is about something these passages never mention."""
    result = extract_evidence(
        "Youth homelessness in Western Sydney rose to 13.1% in 2025.",
        [DESTINATIONS, METHOD],
        model=live_model,
    )

    assert result.evidence == (), [found.evidence for found in result.evidence]


def test_the_reply_names_the_model_that_answered(live_model: EvidenceModel) -> None:
    result = extract_evidence(
        "Sustained destinations were 64%.", [DESTINATIONS], model=live_model
    )

    assert result.model
    assert result.model.strip() == result.model


def test_the_configured_gemini_model_is_in_the_api_model_list(
    live_env: dict[str, str],
) -> None:
    """The group code names gemini-3.5-flash, unverified; nothing is a default
    until a key has listed it. This checks whatever GEMINI_MODEL says."""
    if live_env[PROVIDER_ENV_VAR] != "gemini":
        pytest.skip("only Gemini's model id is checked against a list")
    from google import genai

    key = next(
        live_env[name].strip()
        for name in KEYS["gemini"]
        if live_env.get(name, "").strip()
    )
    configured = live_env.get("GEMINI_MODEL", "").strip()
    assert configured, "GEMINI_MODEL is not set"

    client = genai.Client(api_key=key, vertexai=False)
    listed = {
        (model.name or "").removeprefix("models/") for model in client.models.list()
    }

    assert configured.removeprefix("models/") in listed, sorted(listed)
