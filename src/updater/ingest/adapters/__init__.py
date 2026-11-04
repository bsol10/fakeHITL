"""Format adapters, one per media type (BUILD_SPEC.md section 3).

PPTX, TXT/MD and images are not built yet; their locator variants already
exist in :mod:`updater.models`, so each needs only a module here. XLSX, CSV and
DOCX tables share :mod:`updater.ingest.tabular`, which owns the serialisation.
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
