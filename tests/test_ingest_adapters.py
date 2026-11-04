"""Adapter tests (BUILD_SPEC.md section 3).

Every test here asserts the *locator*, not just the text. Text extraction
failing is loud; a page number off by one is not, and produces plausible
citations that point at the wrong page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    PAGE_ONE,
    PAGE_TWO,
    SENTINEL_MARKERS,
    block_locators,
    dom_locators,
    make_source,
    page_locators,
    write_docx,
    write_html,
    write_pdf,
    write_scanned_pdf,
    write_sentinel_pdf,
)
from updater.ingest import NeedsOCR, extract
from updater.models import MediaType

# PDF


def test_pdf_passages_are_page_anchored(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "destinations.pdf", [PAGE_ONE, PAGE_TWO])

    passages = extract(path, make_source(media_type=MediaType.PDF))

    assert [locator.page for locator in page_locators(passages)] == [1, 2]
    # The content really is on the page its locator claims.
    assert "Sustained destinations" in passages[0].text
    assert "matched administrative records" in passages[1].text


def test_pdf_skips_blank_pages_without_renumbering(tmp_path: Path) -> None:
    """A blank page has nothing citable, but page 3 must still say page 3."""
    path = write_pdf(tmp_path / "gappy.pdf", [PAGE_ONE, [], PAGE_TWO])

    passages = extract(path, make_source(media_type=MediaType.PDF))

    assert [locator.page for locator in page_locators(passages)] == [1, 3]
    assert "matched administrative records" in passages[1].text


def test_a_page_passage_holds_no_text_from_its_neighbours(tmp_path: Path) -> None:
    """Page 2's text being present would still pass if the reader had leaked
    pages 1 and 3 in alongside it. The absence is what makes the anchor
    unambiguous."""
    path = write_sentinel_pdf(tmp_path / "three-pages.pdf")

    passages = extract(path, make_source(media_type=MediaType.PDF))
    locators = page_locators(passages)

    assert [locator.page for locator in locators] == [1, 2, 3]
    for passage, locator in zip(passages, locators, strict=True):
        own_marker = SENTINEL_MARKERS[locator.page - 1]
        assert own_marker in passage.text
        for other in SENTINEL_MARKERS:
            if other != own_marker:
                assert other not in passage.text, (
                    f"page {locator.page} leaked {other} into its passage"
                )


def test_pdf_anchor_spans_the_whole_page(tmp_path: Path) -> None:
    """At ingest the passage is the locator unit, so the span is the whole text."""
    path = write_pdf(tmp_path / "destinations.pdf", [PAGE_ONE])

    passage = extract(path, make_source(media_type=MediaType.PDF))[0]

    assert passage.anchor.char_start == 0
    assert passage.anchor.char_end == len(passage.text)
    assert passage.anchor.source_id == passage.source_id == "dfe-destinations-2025"


def test_scanned_pdf_raises_needs_ocr(tmp_path: Path) -> None:
    path = write_scanned_pdf(tmp_path / "scan.pdf", page_count=3)

    with pytest.raises(NeedsOCR) as caught:
        extract(path, make_source(media_type=MediaType.PDF))

    error = caught.value
    assert error.page_count == 3
    assert error.mean_chars_per_page < error.threshold
    # The message has to tell a human what to do about it.
    assert "ocrmypdf" in str(error)


# DOCX


def test_docx_paragraph_index_addresses_the_real_document(tmp_path: Path) -> None:
    """Index 2 is an empty paragraph. Skipping it must not renumber index 3:
    dense renumbering still produces citations, to the wrong paragraph."""
    path = write_docx(tmp_path / "minutes.docx")

    passages = extract(path, make_source(media_type=MediaType.DOCX))

    assert [locator.paragraph_index for locator in block_locators(passages)] == [
        0,
        1,
        3,
    ]


def test_docx_keeps_the_documents_own_style_names(tmp_path: Path) -> None:
    path = write_docx(tmp_path / "minutes.docx")

    locators = block_locators(extract(path, make_source(media_type=MediaType.DOCX)))

    assert locators[0].style == "Heading 1"
    assert locators[1].style == "Normal"


# HTML


def test_html_passages_carry_the_heading_trail(tmp_path: Path) -> None:
    """The trail is the durable half of a web citation."""
    path = write_html(tmp_path / "destinations.html")

    passages = extract(path, make_source(media_type=MediaType.HTML))
    by_text = {passage.text: passage for passage in passages}

    method = next(p for t, p in by_text.items() if "matched administrative" in t)
    caveats = next(p for t, p in by_text.items() if "independent training" in t)

    method_locator = dom_locators([method])[0]
    caveats_locator = dom_locators([caveats])[0]

    assert method_locator.heading_trail == ("Post-16 destinations, 2025", "Method")
    # Caveats is a sibling of Method, so the trail replaces it rather than nesting.
    assert caveats_locator.heading_trail == ("Post-16 destinations, 2025", "Caveats")


def test_html_css_path_uniquely_addresses_each_passage(tmp_path: Path) -> None:
    path = write_html(tmp_path / "destinations.html")

    locators = dom_locators(extract(path, make_source(media_type=MediaType.HTML)))
    paths = [locator.css_path for locator in locators]

    assert len(set(paths)) == len(paths), f"css_path is not unique: {paths}"
    assert all(path_.startswith("doc > main > ") for path_ in paths)
    # Second <p> under <main> is the Method paragraph, per trafilatura's tree.
    assert "doc > main > p:nth-of-type(2)" in paths


def test_html_strips_navigation_and_footer(tmp_path: Path) -> None:
    path = write_html(tmp_path / "destinations.html")

    joined = " ".join(
        p.text for p in extract(path, make_source(media_type=MediaType.HTML))
    )

    assert "Crown copyright" not in joined
    assert "About us" not in joined
    assert "Sustained destinations" in joined


# Cross-format


@pytest.mark.parametrize(
    ("filename", "writer", "media_type"),
    [
        ("a.pdf", "pdf", MediaType.PDF),
        ("a.docx", "docx", MediaType.DOCX),
        ("a.html", "html", MediaType.HTML),
    ],
)
def test_every_passage_carries_provenance(
    tmp_path: Path, filename: str, writer: str, media_type: str
) -> None:
    """Design rule 1, at the point it first has to be true."""
    path = tmp_path / filename
    if writer == "pdf":
        write_pdf(path, [PAGE_ONE, PAGE_TWO])
    elif writer == "docx":
        write_docx(path)
    else:
        write_html(path)

    passages = extract(path, make_source(media_type=media_type))

    assert passages
    for passage in passages:
        assert passage.anchor.source_id == "dfe-destinations-2025"
        assert passage.anchor.char_end == len(passage.text)
        assert passage.anchor.extracted_at.tzinfo is not None
        assert passage.token_count >= 1
