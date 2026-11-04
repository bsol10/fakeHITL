"""PDF extraction over the text layer (BUILD_SPEC.md section 3).

One passage per page, anchored by :class:`~updater.models.PageLocator`: the page
is what a reviewer can turn to, and the boundary chunking must not cross. Blank
pages produce no passage and are not renumbered around. A document that is
*mostly* blank raises :class:`~updater.ingest.errors.NeedsOCR`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from updater.ingest.base import build_passage
from updater.ingest.errors import ExtractionFailed, NeedsOCR
from updater.models import MediaType, PageLocator, Passage, Source

__all__ = ["SCANNED_TEXT_LAYER_FLOOR", "PdfExtractor"]

SCANNED_TEXT_LAYER_FLOOR = 100
"""Mean characters per page below which the PDF is treated as scanned."""


class PdfExtractor:
    """Reads the embedded text layer. Does not OCR, and will not pretend to."""

    media_types: ClassVar[set[str]] = {MediaType.PDF}

    def extract(self, path: Path, source: Source) -> list[Passage]:
        pages = self._read_pages(path, source)
        if not pages:
            raise ExtractionFailed(
                path=path, source_id=source.id, reason="contains no pages"
            )

        mean_chars = sum(len(text) for _, text in pages) / len(pages)
        if mean_chars < SCANNED_TEXT_LAYER_FLOOR:
            raise NeedsOCR(
                path=path,
                source_id=source.id,
                page_count=len(pages),
                mean_chars_per_page=mean_chars,
                threshold=SCANNED_TEXT_LAYER_FLOOR,
            )

        extracted_at = datetime.now(UTC)
        return [
            build_passage(
                source=source,
                locator=PageLocator(page=number),
                text=text,
                locator_key=f"page{number}",
                extracted_at=extracted_at,
            )
            for number, text in pages
            if text
        ]

    @staticmethod
    def _read_pages(path: Path, source: Source) -> list[tuple[int, str]]:
        """Page number (1-indexed, as the reader sees it) to stripped text."""
        try:
            reader = PdfReader(str(path))
            return [
                (number, (page.extract_text() or "").strip())
                for number, page in enumerate(reader.pages, start=1)
            ]
        except PdfReadError as exc:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason=f"could not be parsed as a PDF: {exc}",
            ) from exc
