"""Dispatch tests (BUILD_SPEC.md section 3).

The spec's own justification for content sniffing is that "ASIL staff will
upload a .txt that is actually a PDF", so that is the first test here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    PAGE_ONE,
    make_source,
    page_locators,
    write_docx,
    write_html,
    write_pdf,
)
from updater.ingest import (
    ExtractorRegistry,
    UnsupportedMediaType,
    extract,
    sniff_media_type,
)
from updater.ingest.adapters import PdfExtractor
from updater.models import MediaType


def test_a_pdf_named_txt_is_still_a_pdf(tmp_path: Path) -> None:
    """The motivating case from the spec, end to end."""
    path = write_pdf(tmp_path / "notes.txt", [PAGE_ONE])

    assert sniff_media_type(path) == MediaType.PDF

    # Even when the registry entry also has the media type wrong, content wins.
    passages = extract(path, make_source(media_type=MediaType.TXT))
    assert page_locators(passages)[0].page == 1


def test_a_docx_named_pdf_is_still_a_docx(tmp_path: Path) -> None:
    path = write_docx(tmp_path / "minutes.pdf")

    assert sniff_media_type(path) == MediaType.DOCX


def test_html_is_recognised_with_no_extension_at_all(tmp_path: Path) -> None:
    path = write_html(tmp_path / "downloaded-page")

    assert sniff_media_type(path) == MediaType.HTML


def test_plain_text_sniffs_as_text_and_has_no_adapter_yet(tmp_path: Path) -> None:
    """TXT is a Phase 3 bonus format; until then the failure must be explicit."""
    path = tmp_path / "notes.txt"
    path.write_text(
        "Just some notes, no markup and no magic bytes.\n", encoding="utf-8"
    )

    assert sniff_media_type(path) == MediaType.TXT

    with pytest.raises(UnsupportedMediaType) as caught:
        extract(path, make_source(media_type=MediaType.TXT))

    message = str(caught.value)
    assert "text/plain" in message
    # The message lists what we can read, so the reader knows what to do next.
    assert MediaType.PDF in message


def test_markdown_falls_back_to_the_extension(tmp_path: Path) -> None:
    """Markdown and plain text are byte-identical; only the name separates them."""
    path = tmp_path / "notes.md"
    path.write_text("# Heading\n\nSome prose.\n", encoding="utf-8")

    assert sniff_media_type(path) == MediaType.MARKDOWN


def test_registry_refuses_to_shadow_a_registered_media_type() -> None:
    """Two adapters silently competing for application/pdf is a latent bug."""
    registry = ExtractorRegistry()
    registry.register(PdfExtractor())

    with pytest.raises(ValueError, match="already handled by PdfExtractor"):
        registry.register(PdfExtractor())


def test_registering_the_same_instance_twice_is_harmless() -> None:
    registry = ExtractorRegistry()
    extractor = PdfExtractor()

    registry.register(extractor)
    registry.register(extractor)

    assert registry.media_types == frozenset({MediaType.PDF})
