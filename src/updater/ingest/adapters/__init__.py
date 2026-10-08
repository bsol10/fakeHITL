"""Format adapters, one per media type (BUILD_SPEC.md section 3).

PDF, HTML and DOCX were built first because they cover the overwhelming
majority of nonprofit research sources. XLSX and CSV followed immediately after,
out of the spec's stated order, because ASIL's real material turned out to be
mostly sector skills workbooks. PPTX, TXT/MD and images remain the bonus round;
each needs one module here and one locator variant in :mod:`updater.models`, and
nothing else in the pipeline changes.

XLSX and CSV share :mod:`updater.ingest.tabular`, which owns the serialisation.
The parsing differs; what a row *becomes* must not.
"""

from __future__ import annotations

from updater.ingest.adapters.csv import CsvExtractor
from updater.ingest.adapters.docx import DocxExtractor
from updater.ingest.adapters.html import HtmlExtractor
from updater.ingest.adapters.pdf import SCANNED_TEXT_LAYER_FLOOR, PdfExtractor
from updater.ingest.adapters.xlsx import XlsxExtractor

__all__ = [
    "SCANNED_TEXT_LAYER_FLOOR",
    "CsvExtractor",
    "DocxExtractor",
    "HtmlExtractor",
    "PdfExtractor",
    "XlsxExtractor",
]
