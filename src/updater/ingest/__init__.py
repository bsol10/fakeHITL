"""Multi-format extraction (BUILD_SPEC.md section 3).

An adapter registry: one extractor per media type, each returning
``list[Passage]`` with a format-appropriate locator. Dispatch is on the
*sniffed* media type, never the file extension. Every adapter's output passes
the quality gate in :mod:`updater.ingest.quality`; failures are raised and
surfaced as ``extraction-failed``, never returned as empty evidence.

Usage::

    from updater.ingest import extract

    passages = extract(Path("destinations-2025.pdf"), source)

The failure modes are distinct on purpose: :class:`NeedsOCR` is a work item,
:class:`ExtractionFailed` is a bug report, and :class:`UnsupportedMediaType`
means a registered format we cannot read yet.
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
    """The registry with every built-in adapter in it.

    Adapters are imported here, not at module scope: trafilatura pulls in lxml
    and a date parser, about a second of import time that code needing only
    :class:`NeedsOCR` should not pay.
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
    quality gate. Every returned passage carries a `SourceAnchor` (design rule
    1), which must survive all the way to the draft.

    Raises:
        UnsupportedMediaType: nothing is registered for the sniffed type.
        NeedsOCR: the file is a PDF with no usable text layer.
        ExtractionFailed: the adapter ran but its output is not usable.
    """
    return default_registry().extract(path, source)
