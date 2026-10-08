"""Smoke test for the Phase 0 skeleton.

There is no pipeline logic yet, so this covers the things that would invalidate
every later phase if they broke: the package imports, provenance survives a full
object graph (design rule 1), and unstated fields come back null rather than
having to be argued out of a value (design rule 2).
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

import updater
from updater.models import (
    Evidence,
    MediaType,
    PageLocator,
    Passage,
    Source,
    SourceAnchor,
    UpdateFrequency,
)

PIPELINE_MODULES = [
    "updater.models",
    "updater.provenance",
    "updater.registry",
    "updater.ingest",
    "updater.retrieve",
    "updater.extract",
    "updater.compare",
    "updater.draft",
    "updater.eval",
]

PASSAGE_TEXT = "Sustained destinations rose to 68% of the cohort."


def _anchor() -> SourceAnchor:
    return SourceAnchor(
        source_id="dfe-destinations-2025",
        locator=PageLocator(page=14),
        char_start=0,
        char_end=len(PASSAGE_TEXT),
        extracted_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_package_exposes_version() -> None:
    assert updater.__version__


@pytest.mark.parametrize("name", PIPELINE_MODULES)
def test_every_pipeline_stage_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_provenance_survives_to_evidence() -> None:
    """Design rule 1: text carries its anchor from extraction to draft.

    Assembled the way the pipeline will assemble it — source, anchor, passage,
    evidence — then read back from the far end. Nothing in the chain is allowed
    to hand on a bare string.
    """
    source = Source(
        id="dfe-destinations-2025",
        title="Destinations of KS4 and KS5 students",
        url_or_path="https://example.org/destinations-2025.pdf",
        owner="Department for Education",
        topic="post-16 outcomes",
        year=2025,
        update_frequency=UpdateFrequency.ANNUAL,
        media_type=MediaType.PDF,
    )
    anchor = _anchor()
    passage = Passage(
        id="dfe-destinations-2025:p14:0",
        source_id=source.id,
        text=PASSAGE_TEXT,
        anchor=anchor,
        token_count=11,
    )
    evidence = Evidence(
        finding="Sustained destinations rose year on year.",
        statistic="68%",
        anchor=passage.anchor,
        confidence=0.82,
    )

    assert evidence.anchor.source_id == source.id

    # Consumers narrow the locator union by isinstance; there is no bare .page.
    locator = evidence.anchor.locator
    assert isinstance(locator, PageLocator)
    assert locator.page == 14

    # The supporting span is derived from the anchor, never stored alongside it.
    span = passage.text[evidence.anchor.char_start : evidence.anchor.char_end]
    assert span == PASSAGE_TEXT


def test_unstated_fields_stay_null() -> None:
    """Design rule 2: null is the default, not a special case.

    The real version of this test belongs to the extraction stage, feeding
    passages that contain no statistic and asserting none is invented. This is
    the constructor-level precondition for it: omitting a field must be less
    work than supplying one.
    """
    evidence = Evidence(
        finding="Participation is described as broadly stable.",
        anchor=_anchor(),
        confidence=0.4,
    )

    assert evidence.statistic is None
    assert evidence.method is None
    assert evidence.as_of_date is None
    assert evidence.caveat is None


def test_anchor_rejects_a_reversed_span() -> None:
    with pytest.raises(ValueError, match="precedes char_start"):
        SourceAnchor(
            source_id="dfe-destinations-2025",
            locator=PageLocator(page=14),
            char_start=48,
            char_end=0,
            extracted_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
