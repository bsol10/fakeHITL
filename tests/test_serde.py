"""Locators surviving the trip through JSON and SQLite.

A locator read back from the index must be the same locator that was written,
not a similar one. The ``kind`` tag on each variant is what makes that possible
in a database column, which has no ``isinstance``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from updater.models import (
    BlockLocator,
    CellLocator,
    DomLocator,
    Locator,
    PageLocator,
    SlideLocator,
    SourceAnchor,
    TimeLocator,
)
from updater.serde import (
    LOCATOR_KINDS,
    SerdeError,
    anchor_from_row,
    locator_from_json,
    locator_to_json,
)

EVERY_LOCATOR: list[Locator] = [
    PageLocator(page=14),
    BlockLocator(paragraph_index=194, style="Heading 2"),
    BlockLocator(paragraph_index=0, style=""),
    CellLocator(sheet="Salary Premiums", cell_range="A13:D13"),
    CellLocator(sheet="Table 3 (para 194)", cell_range="B4"),
    SlideLocator(slide=7, shape_index=2),
    DomLocator(css_path="doc > main > p:nth-of-type(2)", heading_trail=()),
    DomLocator(
        css_path="doc > main > p:nth-of-type(5)",
        heading_trail=("Post-16 destinations, 2025", "Method"),
    ),
    TimeLocator(start_sec=12.5, end_sec=48.0),
]


@pytest.mark.parametrize("locator", EVERY_LOCATOR, ids=lambda loc: repr(loc)[:40])
def test_every_locator_variant_round_trips(locator: Locator) -> None:
    assert locator_from_json(locator_to_json(locator)) == locator


def test_the_parametrised_cases_cover_every_variant() -> None:
    """So adding a locator variant without a round-trip test fails here."""
    covered = {locator.kind for locator in EVERY_LOCATOR}

    assert covered == LOCATOR_KINDS


def test_a_dom_heading_trail_comes_back_as_a_tuple() -> None:
    """JSON has no tuples. A list here makes the frozen anchor unhashable, which
    fails somewhere else entirely and looks unrelated to serialisation."""
    locator = DomLocator(css_path="doc > p", heading_trail=("Method", "Caveats"))

    restored = locator_from_json(locator_to_json(locator))

    assert isinstance(restored, DomLocator)
    assert isinstance(restored.heading_trail, tuple)
    hash(restored)


def test_serialisation_is_stable_for_equal_locators() -> None:
    """Keys are sorted, so a stored locator can be compared or deduplicated as
    text without being parsed first."""
    first = CellLocator(sheet="Skills gaps", cell_range="A7:D7")
    second = CellLocator(cell_range="A7:D7", sheet="Skills gaps")

    assert locator_to_json(first) == locator_to_json(second)


def test_an_unknown_kind_is_refused_rather_than_guessed() -> None:
    with pytest.raises(SerdeError, match="unknown locator kind"):
        locator_from_json('{"kind": "paragraph", "index": 3}')


def test_a_missing_kind_is_refused() -> None:
    with pytest.raises(SerdeError, match="no 'kind' tag"):
        locator_from_json('{"page": 3}')


def test_a_payload_that_does_not_match_its_kind_is_refused() -> None:
    """A locator that quietly defaults a missing field is a guess about
    provenance, which is the same failure as a confabulated statistic."""
    with pytest.raises(SerdeError, match="does not match"):
        locator_from_json('{"kind": "cell", "sheet": "Sheet1"}')


def test_anchors_round_trip_through_stored_columns() -> None:
    anchor = SourceAnchor(
        source_id="asil-jobs-of-the-future",
        locator=CellLocator(sheet="Table 1 (para 5)", cell_range="A2:D2"),
        char_start=17,
        char_end=94,
        extracted_at=datetime(2026, 9, 7, 12, 30, tzinfo=UTC),
    )

    restored = anchor_from_row(
        source_id=anchor.source_id,
        locator_json=locator_to_json(anchor.locator),
        char_start=anchor.char_start,
        char_end=anchor.char_end,
        extracted_at=anchor.extracted_at.isoformat(),
    )

    assert restored == anchor


def test_a_broken_timestamp_is_refused() -> None:
    with pytest.raises(SerdeError, match="ISO-8601"):
        anchor_from_row(
            source_id="s",
            locator_json='{"kind": "page", "page": 1}',
            char_start=0,
            char_end=1,
            extracted_at="last Tuesday",
        )
