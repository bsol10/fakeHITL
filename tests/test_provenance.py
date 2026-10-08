"""Tests for the chunk-boundary invariant (BUILD_SPEC.md section 4).

Chunking does not exist yet. That is exactly why these are written now: the rule
"never span two locators" is easy to hold to when the check is already named and
tested, and easy to relax when it is a sentence in a spec.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from updater.models import BlockLocator, PageLocator, SourceAnchor
from updater.provenance import (
    ProvenanceError,
    derives_within,
    require_derives_within,
    same_locator_unit,
)

EXTRACTED_AT = datetime(2026, 8, 21, tzinfo=UTC)


def anchor(
    *,
    page: int = 2,
    start: int = 0,
    end: int = 400,
    source_id: str = "dfe-destinations-2025",
) -> SourceAnchor:
    return SourceAnchor(
        source_id=source_id,
        locator=PageLocator(page=page),
        char_start=start,
        char_end=end,
        extracted_at=EXTRACTED_AT,
    )


PAGE_TWO = anchor()


def test_a_chunk_inside_the_page_derives_within_it() -> None:
    assert derives_within(PAGE_TWO, anchor(start=100, end=250))


def test_a_page_derives_within_itself() -> None:
    """Ingest's own output has to satisfy the rule it hands to chunking."""
    assert derives_within(PAGE_TWO, anchor())


def test_a_chunk_overrunning_the_end_of_the_page_does_not() -> None:
    assert not derives_within(PAGE_TWO, anchor(start=300, end=520))


def test_a_chunk_starting_before_its_parent_does_not() -> None:
    parent = anchor(start=100, end=400)

    assert not derives_within(parent, anchor(start=50, end=200))


def test_a_chunk_anchored_to_the_next_page_does_not() -> None:
    """The case the spec calls out by name: a chunk spanning two PDF pages."""
    assert not derives_within(PAGE_TWO, anchor(page=3, end=200))


def test_a_chunk_from_a_different_source_does_not() -> None:
    assert not derives_within(PAGE_TWO, anchor(source_id="ons-lfs-2025", end=200))


def test_position_two_of_another_locator_variant_is_a_different_unit() -> None:
    """Paragraph 2 and page 2 are both "position 2", and are not the same place."""
    paragraph_two = SourceAnchor(
        source_id="dfe-destinations-2025",
        locator=BlockLocator(paragraph_index=2, style="Normal"),
        char_start=0,
        char_end=400,
        extracted_at=EXTRACTED_AT,
    )

    assert not same_locator_unit(PAGE_TWO, paragraph_two)


def test_require_is_silent_for_a_valid_narrowing() -> None:
    require_derives_within(PAGE_TWO, anchor(start=10, end=20))


def test_require_names_the_unit_when_a_chunk_crosses_pages() -> None:
    with pytest.raises(ProvenanceError, match="different locator unit"):
        require_derives_within(PAGE_TWO, anchor(page=3))


def test_require_names_the_span_when_a_chunk_overruns() -> None:
    with pytest.raises(ProvenanceError, match="escapes the parent"):
        require_derives_within(PAGE_TWO, anchor(start=300, end=900))
