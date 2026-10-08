"""Chunking: the stage the provenance invariant was written for.

The thing that goes wrong here is not a crash. It is a chunk whose text and
whose anchor disagree — text from page 8 carrying a citation to page 7 — and it
produces a draft that reads perfectly and cites the wrong place. No amount of
"the chunk contains what I expect" testing catches it, because the leaked text
is *also* in the document.

So the assertions here are about **absence**. Every region of the fixture
carries a unique marker, and each chunk is asked to prove it holds no marker
from outside the span its anchor claims. Two mutants at the bottom of the file
prove that check has teeth: one smears neighbouring text into a chunk while
leaving the anchor alone, the other shifts the anchor while leaving the text
alone. Both are invisible to a positive containment test, and the PDF page tests
learned the same lesson the same way.

Most tests here use a whitespace word counter rather than the model's
tokenizer. Boundaries are what is under test, not tokenization, and a failure
message reading "chunk has 7 words" can be checked by eye.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

import pytest

from conftest import (
    MARKER_COUNT,
    WordCounter,
    assert_chunk_slices_back,
    make_passage,
    marker,
    sentinel_text,
)
from updater.models import BlockLocator, CellLocator, PageLocator, Passage
from updater.provenance import ProvenanceError, derives_within
from updater.retrieve.chunk import (
    ChunkConfig,
    _overlap_start,
    chunk_passage,
    chunk_passages,
    split_spans,
)
from updater.retrieve.tokenize import TokenizerUnavailable, default_token_counter

ALL_MARKERS = frozenset(marker(index) for index in range(MARKER_COUNT))

SMALL = ChunkConfig(max_tokens=40, min_tokens=25, overlap_ratio=0.15)
"""Small enough that the fixture splits into a useful number of chunks."""


def markers_in(text: str) -> frozenset[str]:
    return frozenset(name for name in ALL_MARKERS if name in text)


def assert_holds_only_its_own_markers(parent: Passage, chunk: Passage) -> None:
    """The absence assertion.

    Every marker the chunk's anchor covers must be in its text, and no marker it
    does not cover may be. The second half is the one that matters: it is what a
    chunker that quietly borrows a neighbouring sentence fails.
    """
    offset = parent.anchor.char_start
    span = parent.text[
        chunk.anchor.char_start - offset : chunk.anchor.char_end - offset
    ]
    covered = markers_in(span)

    assert markers_in(chunk.text) >= covered, (
        f"{chunk.id} is missing markers its anchor claims: "
        f"{sorted(covered - markers_in(chunk.text))}"
    )
    foreign = markers_in(chunk.text) - covered
    assert not foreign, (
        f"{chunk.id} holds text from outside its own span: {sorted(foreign)}. "
        f"Its anchor claims [{chunk.anchor.char_start}, {chunk.anchor.char_end})."
    )


# --------------------------------------------------------------------------- #
# The properties every chunk has
# --------------------------------------------------------------------------- #


def test_a_passage_within_budget_becomes_one_chunk_over_its_whole_unit() -> None:
    """Nothing is split unnecessarily, and the single chunk covers the unit.

    This is what makes the inspector's "whole unit" line meaningful: the first
    thing that prints PARTIAL is a chunk that genuinely was split.
    """
    passage = make_passage("Sustained destinations rose to 68% of the cohort.")

    chunks = chunk_passage(passage, counter=WordCounter(), config=SMALL)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text == passage.text
    assert chunk.anchor.char_start == passage.anchor.char_start
    assert chunk.anchor.char_end == passage.anchor.char_end
    assert chunk.id == "src:page7#0"


def test_every_chunk_is_a_verbatim_slice_of_its_parent() -> None:
    """No normalisation, no re-joining. The extract stage verifies quoted spans
    by plain string match against this text; rewriting it here would make that
    check meaningless."""
    passage = make_passage(sentinel_text())

    for chunk in chunk_passage(passage, counter=WordCounter(), config=SMALL):
        assert_chunk_slices_back(passage, chunk)


def test_a_chunk_holds_no_text_from_outside_its_own_span() -> None:
    passage = make_passage(sentinel_text())

    chunks = chunk_passage(passage, counter=WordCounter(), config=SMALL)

    assert len(chunks) > 3, "fixture should split into several chunks"
    for chunk in chunks:
        assert_holds_only_its_own_markers(passage, chunk)


def test_every_chunk_derives_within_its_parent() -> None:
    passage = make_passage(sentinel_text())

    for chunk in chunk_passage(passage, counter=WordCounter(), config=SMALL):
        assert derives_within(passage.anchor, chunk.anchor)


def test_chunks_keep_the_parents_locator_exactly() -> None:
    """A chunk of page 7 is anchored to page 7 and to nothing else. This is the
    rule BUILD_SPEC.md section 4 states, at the level the type system cannot."""
    passage = make_passage(sentinel_text(), locator=PageLocator(page=7))

    for chunk in chunk_passage(passage, counter=WordCounter(), config=SMALL):
        assert chunk.anchor.locator == PageLocator(page=7)
        assert chunk.source_id == passage.source_id


@pytest.mark.parametrize(
    "locator",
    [
        PageLocator(page=3),
        BlockLocator(paragraph_index=41, style="Heading 2"),
        CellLocator(sheet="Skills gaps", cell_range="A7:D7"),
    ],
)
def test_the_locator_is_carried_unchanged_whatever_the_format(
    locator: PageLocator | BlockLocator | CellLocator,
) -> None:
    passage = make_passage(sentinel_text(), locator=locator)

    kinds = {
        chunk.anchor.locator
        for chunk in chunk_passage(passage, counter=WordCounter(), config=SMALL)
    }

    assert kinds == {locator}


def test_no_chunk_exceeds_the_token_budget() -> None:
    counter = WordCounter()
    passage = make_passage(sentinel_text())

    for chunk in chunk_passage(passage, counter=counter, config=SMALL):
        assert counter.count(chunk.text) <= SMALL.max_tokens


def test_the_chunks_cover_the_whole_passage() -> None:
    """Nothing is dropped. A silently missing tail is an absence of evidence
    downstream, which the compare stage reads as a finding about the world."""
    passage = make_passage(sentinel_text())

    chunks = chunk_passage(passage, counter=WordCounter(), config=SMALL)

    covered: set[str] = set()
    for chunk in chunks:
        covered |= markers_in(chunk.text)
    assert covered == ALL_MARKERS


def test_consecutive_chunks_overlap() -> None:
    passage = make_passage(sentinel_text())

    chunks = chunk_passage(passage, counter=WordCounter(), config=SMALL)

    overlaps = [
        earlier.anchor.char_end - later.anchor.char_start
        for earlier, later in pairwise(chunks)
    ]
    assert all(overlap > 0 for overlap in overlaps), (
        f"expected every pair to overlap, got {overlaps}"
    )


def test_zero_overlap_gives_a_partition() -> None:
    passage = make_passage(sentinel_text())
    config = ChunkConfig(max_tokens=40, min_tokens=1, overlap_ratio=0.0)

    chunks = chunk_passage(passage, counter=WordCounter(), config=config)

    for earlier, later in pairwise(chunks):
        assert earlier.anchor.char_end <= later.anchor.char_start


def test_chunk_ids_are_derived_and_unique() -> None:
    passage = make_passage(sentinel_text(), passage_id="report:para12")

    chunks = chunk_passage(passage, counter=WordCounter(), config=SMALL)

    assert [chunk.id for chunk in chunks] == [
        f"report:para12#{index}" for index in range(len(chunks))
    ]


def test_a_stub_tail_is_grown_backwards_rather_than_left_tiny() -> None:
    """Greedy packing leaves the remainder in the last chunk, which can be a
    handful of words. A stub embeds badly, so the tail of every long page would
    become quietly unfindable."""
    counter = WordCounter()
    config = ChunkConfig(max_tokens=40, min_tokens=30, overlap_ratio=0.15)
    passage = make_passage(sentinel_text(count=11, filler_words=12))

    chunks = chunk_passage(passage, counter=counter, config=config)

    assert counter.count(chunks[-1].text) >= config.min_tokens
    assert chunks[-1].anchor.char_start > chunks[-2].anchor.char_start
    assert_chunk_slices_back(passage, chunks[-1])


def test_whitespace_only_passages_produce_no_chunks() -> None:
    assert chunk_passage(make_passage("   \n\n  "), counter=WordCounter()) == []


def test_chunking_a_chunk_stays_inside_the_original_unit() -> None:
    """Offsets are locator-relative, so a chunk of a chunk has to add its
    parent's start rather than restart from zero. If that arithmetic were wrong
    the citation would point at the top of the page instead of the middle."""
    passage = make_passage(sentinel_text())
    first_pass = chunk_passage(passage, counter=WordCounter(), config=SMALL)
    middle = first_pass[len(first_pass) // 2]

    grandchildren = chunk_passage(
        middle,
        counter=WordCounter(),
        config=ChunkConfig(max_tokens=15, min_tokens=8, overlap_ratio=0.15),
    )

    assert len(grandchildren) > 1
    for grandchild in grandchildren:
        assert derives_within(middle.anchor, grandchild.anchor)
        assert derives_within(passage.anchor, grandchild.anchor)
        assert_chunk_slices_back(middle, grandchild)


def test_chunk_passages_preserves_order_across_passages() -> None:
    passages = [
        make_passage(
            "First page text.", passage_id="src:page1", locator=PageLocator(page=1)
        ),
        make_passage(
            "Second page text.", passage_id="src:page2", locator=PageLocator(page=2)
        ),
    ]

    chunks = chunk_passages(passages, counter=WordCounter(), config=SMALL)

    assert [chunk.anchor.locator for chunk in chunks] == [
        PageLocator(page=1),
        PageLocator(page=2),
    ]


# --------------------------------------------------------------------------- #
# The guard, and the mutants that prove the assertions have teeth
# --------------------------------------------------------------------------- #


def test_a_chunk_escaping_its_parent_raises_rather_than_being_returned() -> None:
    """The parent here claims to cover only its first 20 characters, so any
    chunk beyond that escapes it. A real chunker cannot produce this, which is
    exactly why the guard has to be tested with a rigged parent — the check must
    fire before a wrong citation is returned, not after somebody reads it."""
    text = sentinel_text()
    passage = make_passage(text, char_end=20)

    with pytest.raises(ProvenanceError, match="escapes the parent"):
        chunk_passage(passage, counter=WordCounter(), config=SMALL)


def test_the_absence_assertion_catches_a_chunk_that_smears_its_neighbour() -> None:
    """Mutant 1: correct anchor, text borrowed from beyond it.

    This is the failure that produces a plausible sentence with a wrong
    citation. A positive containment test passes it happily.
    """
    passage = make_passage(sentinel_text())
    chunk = chunk_passage(passage, counter=WordCounter(), config=SMALL)[0]
    end = chunk.anchor.char_end
    smeared = replace(chunk, text=passage.text[chunk.anchor.char_start : end + 400])

    with pytest.raises(AssertionError, match="outside its own span"):
        assert_holds_only_its_own_markers(passage, smeared)


def test_the_slice_assertion_catches_an_anchor_shifted_off_its_text() -> None:
    """Mutant 2: correct text, anchor moved.

    The mirror image of mutant 1, and the reason both assertions exist: this one
    keeps every marker the chunk should have and still cites the wrong offsets.
    """
    passage = make_passage(sentinel_text())
    chunk = chunk_passage(passage, counter=WordCounter(), config=SMALL)[1]
    shifted = replace(
        chunk,
        anchor=replace(
            chunk.anchor,
            char_start=chunk.anchor.char_start - 30,
            char_end=chunk.anchor.char_end - 30,
        ),
    )

    with pytest.raises(AssertionError, match="says it covers"):
        assert_chunk_slices_back(passage, shifted)


def test_overlap_start_always_advances() -> None:
    """The packing loop terminates only because the next chunk starts strictly
    later than the one just emitted. An off-by-one here is an infinite loop, not
    a wrong answer, so it gets its own test."""
    sizes = [1] * 20

    for first in range(0, 19):
        for stop in range(first + 1, 20):
            start = _overlap_start(sizes, first, stop, overlap_tokens=1000)
            assert first < start <= stop


# --------------------------------------------------------------------------- #
# The real tokenizer — the budget is in the embedding model's units
# --------------------------------------------------------------------------- #


def test_real_word_piece_counts_respect_the_budget() -> None:
    try:
        counter = default_token_counter()
    except TokenizerUnavailable as error:  # pragma: no cover - depends on install
        pytest.skip(f"embedding tokenizer unavailable: {error}")

    config = ChunkConfig(max_tokens=120, min_tokens=80, overlap_ratio=0.15)
    passage = make_passage(sentinel_text(count=30))

    chunks = chunk_passage(passage, counter=counter, config=config)

    assert len(chunks) > 1
    for chunk in chunks:
        assert counter.count(chunk.text) <= config.max_tokens
        assert chunk.token_count == counter.count(chunk.text)
        assert_chunk_slices_back(passage, chunk)
        assert_holds_only_its_own_markers(passage, chunk)


def test_split_spans_are_valid_slices_in_order() -> None:
    text = sentinel_text()

    spans = split_spans(text, counter=WordCounter(), config=SMALL)

    assert spans == sorted(spans)
    for start, end in spans:
        assert 0 <= start < end <= len(text)
        assert text[start:end].strip() == text[start:end], "spans are trimmed"
