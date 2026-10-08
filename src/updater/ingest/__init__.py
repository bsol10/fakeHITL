"""Multi-format extraction (BUILD_SPEC.md section 3).

An adapter registry: one extractor per media type, every one of them returning
``list[Passage]`` with a format-appropriate locator. Adding a format means
adding one adapter and one locator variant — nothing downstream changes. That is
the whole point of the design.

Dispatch is on the *sniffed* media type, never the file extension. ASIL staff
will upload a ``.txt`` that is actually a PDF.

Each adapter's output passes an extraction quality gate before the passages are
accepted (non-empty, alphanumeric ratio at or above 0.6, no "word" over 50
characters). Failures are logged against the source and surfaced as
``extraction-failed``; they never become silently empty evidence.

Build order: PDF, HTML and DOCX first, then XLSX and CSV — ASIL's own material
is mostly sector skills workbooks, which moved the tabular formats ahead of the
spec's stated priority.

Usage::

    from updater.ingest import extract

    passages = extract(Path("destinations-2025.pdf"), source)

Everything below :func:`extract` is exported for the tests and for the CLI,
which has to tell the failure modes apart: :class:`NeedsOCR` is a work item,
:class:`ExtractionFailed` is a bug report, and :class:`UnsupportedMediaType`
means somebody registered a format we cannot read yet.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from updater.ingest.base import (
    Extractor,
    ExtractorRegistry,
    build_passage,
    estimate_tokens,
)
from updater.ingest.errors import (
    EXTRACTION_FAILED_STATUS,
    ExtractionFailed,
    IngestError,
    NeedsOCR,
    UnsupportedMediaType,
)
from updater.ingest.quality import (
    ALNUM_RATIO_FLOOR,
    MAX_TOKEN_LENGTH,
    QualityFailure,
    QualityIssue,
    apply_quality_gate,
    check_passage,
)
from updater.ingest.sniff import sniff_media_type

if TYPE_CHECKING:
    from updater.models import Passage, Source

__all__ = [
    "ALNUM_RATIO_FLOOR",
    "EXTRACTION_FAILED_STATUS",
    "MAX_TOKEN_LENGTH",
    "ExtractionFailed",
    "Extractor",
    "ExtractorRegistry",
    "IngestError",
    "NeedsOCR",
    "QualityFailure",
    "QualityIssue",
    "UnsupportedMediaType",
    "apply_quality_gate",
    "build_passage",
    "check_passage",
    "default_registry",
    "estimate_tokens",
    "extract",
    "sniff_media_type",
]


@lru_cache(maxsize=1)
def default_registry() -> ExtractorRegistry:
    """The registry with the Phase 1 adapters already in it.

    Adapters are imported inside the function, not at module scope, because
    trafilatura drags in lxml and a date parser — about a second of import time
    that code reaching for :class:`NeedsOCR` alone should not have to pay.
    """
    from updater.ingest.adapters import (
        CsvExtractor,
        DocxExtractor,
        HtmlExtractor,
        PdfExtractor,
        XlsxExtractor,
    )

    registry = ExtractorRegistry()
    extractors = (
        PdfExtractor(),
        HtmlExtractor(),
        DocxExtractor(),
        XlsxExtractor(),
        CsvExtractor(),
    )
    for extractor in extractors:
        registry.register(extractor)
    return registry


def extract(path: Path, source: Source) -> list[Passage]:
    """Extract anchored passages from one source file.

    Sniffs the media type, dispatches to the registered adapter, and applies the
    quality gate. Every returned passage carries a `SourceAnchor` — design rule
    1 starts being true here and must stay true all the way to the draft.

    Raises:
        UnsupportedMediaType: nothing is registered for the sniffed type.
        NeedsOCR: the file is a PDF with no usable text layer.
        ExtractionFailed: the adapter ran but its output is not usable.
    """
    return default_registry().extract(path, source)
