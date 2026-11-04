"""Failure modes of the ingestion layer.

Extraction problems are raised and surfaced as ``extraction-failed``
(BUILD_SPEC.md section 3), never returned as an empty list. Downstream, a source
that extracted to nothing looks identical to one that genuinely says nothing
about a claim; the second is a finding, the first is a bug.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from updater.ingest.quality import QualityFailure

__all__ = [
    "EXTRACTION_FAILED_STATUS",
    "ExtractionFailed",
    "IngestError",
    "NeedsOCR",
    "UnsupportedMediaType",
]

EXTRACTION_FAILED_STATUS = "extraction-failed"
"""Status the review UI shows for a source this layer could not read."""


class IngestError(Exception):
    """Base class for every ingestion failure."""


class UnsupportedMediaType(IngestError):
    """Sniffing succeeded, but no adapter is registered for the result."""

    def __init__(
        self,
        *,
        path: Path,
        media_type: str,
        registered: Iterable[str],
    ) -> None:
        self.path = path
        self.media_type = str(media_type)
        self.registered = tuple(sorted(str(item) for item in registered))
        known = ", ".join(self.registered) or "none"
        super().__init__(
            f"{path} sniffed as {self.media_type!r}, which no registered extractor "
            f"handles. Registered media types: {known}."
        )


class NeedsOCR(IngestError):
    """The PDF's text layer is too thin to be real; it needs OCR first.

    A scanned page extracts to a few stray characters rather than to nothing,
    so without this check it would pass as a short page and contribute no
    evidence. OCR (``ocrmypdf``, BUILD_SPEC.md section 3) is not implemented
    yet; this error is the handoff point.
    """

    def __init__(
        self,
        *,
        path: Path,
        source_id: str,
        page_count: int,
        mean_chars_per_page: float,
        threshold: int,
    ) -> None:
        self.path = path
        self.source_id = source_id
        self.page_count = page_count
        self.mean_chars_per_page = mean_chars_per_page
        self.threshold = threshold
        super().__init__(
            f"{path} (source {source_id!r}) has a text layer averaging "
            f"{mean_chars_per_page:.1f} characters over {page_count} page(s), "
            f"below the {threshold}-character threshold, so it looks scanned. "
            f"OCR it first (ocrmypdf) and ingest the result. OCR is not "
            f"implemented yet."
        )


class ExtractionFailed(IngestError):
    """An adapter ran but its output is not usable as evidence.

    Carries the individual quality-gate verdicts, if any, so a reviewer can see
    *which* passage failed and why. Maps to :data:`EXTRACTION_FAILED_STATUS`.
    """

    def __init__(
        self,
        *,
        path: Path,
        source_id: str,
        reason: str,
        failures: Sequence[QualityFailure] = (),
    ) -> None:
        self.path = path
        self.source_id = source_id
        self.reason = reason
        self.failures = tuple(failures)
        self.status = EXTRACTION_FAILED_STATUS
        detail = "".join(f"\n  - {failure}" for failure in self.failures)
        super().__init__(f"{path} (source {source_id!r}) {reason}{detail}")
