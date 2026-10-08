"""Media-type sniffing (BUILD_SPEC.md section 3).

Dispatch is on content, never on the file extension, because ASIL staff will
upload a ``.txt`` that is actually a PDF and the pipeline must not care.

``filetype`` covers the formats that have magic bytes. Plain-text formats do
not have any, so they fall through to a marker scan over the first few
kilobytes. The one place an extension is consulted is the final tiebreak between
Markdown and plain text, which are byte-identical formats that no amount of
content sniffing can separate — and by then every binary format has already been
ruled out on content, so the spec's rule still holds where it matters.
"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import filetype

from updater.models import MediaType

__all__ = [
    "CSV_DELIMITERS",
    "SNIFF_BYTES",
    "detect_delimiter",
    "sniff_media_type",
]

SNIFF_BYTES = 4096
"""How much of a text file to scan for format markers."""

_ZIP_MIME = "application/zip"

_OOXML_PART_MARKERS: tuple[tuple[str, str], ...] = (
    ("word/document.xml", MediaType.DOCX),
    ("xl/workbook.xml", MediaType.XLSX),
    ("ppt/presentation.xml", MediaType.PPTX),
)
"""Every OOXML file is a zip; the part names are what distinguish them."""

_HTML_MARKERS = (
    "<!doctype html",
    "<html",
    "<head",
    "<body",
    "<article",
    "<section",
    "<div",
    "<p>",
    "<p ",
)

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

CSV_DELIMITERS = (",", ";", "\t", "|")
"""Delimiters worth testing, commonest first.

Semicolon is not exotic: it is what Excel writes on a machine with a European
locale, where the comma is the decimal separator. Tab-separated files sniff as
``text/csv`` too — there is no TSV media type in the spec and the adapter reads
whichever delimiter this module reports, so a separate type would buy nothing
but a second dispatch key pointing at the same code.
"""

CSV_SAMPLE_LINES = 20
"""Lines of the head to test for a consistent field count."""

CSV_MIN_LINES = 2
"""Below two lines there is no consistency to observe, so it is not a table."""

CSV_MIN_FIELDS = 2
"""One field per line means no delimiter is doing any work."""


def sniff_media_type(path: Path) -> str:
    """Return the IANA media type of ``path``, judged by its content.

    Always a plain ``str``, never a :class:`~updater.models.MediaType` member.
    The constants are a convenience for writing this module; letting one escape
    would put ``<MediaType.TXT: 'text/plain'>`` into error messages meant for
    ASIL staff.
    """
    guess = filetype.guess(str(path))
    if guess is None:
        return str(_sniff_text(path))

    mime = str(guess.mime)
    if mime == _ZIP_MIME:
        # A bare zip may still be a DOCX whose part layout filetype missed.
        refined = _sniff_ooxml_container(path)
        if refined is not None:
            return str(refined)
    return mime


def _sniff_ooxml_container(path: Path) -> str | None:
    """Distinguish DOCX/XLSX/PPTX by the parts inside the zip."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return None

    for marker, media_type in _OOXML_PART_MARKERS:
        if marker in names:
            return media_type
    return None


def _sniff_text(path: Path) -> str:
    """Classify a file with no magic bytes."""
    head = path.read_bytes()[:SNIFF_BYTES]
    decoded = head.decode("utf-8", errors="ignore")
    lowered = decoded.lower()

    if any(marker in lowered for marker in _HTML_MARKERS):
        return MediaType.HTML
    if _delimiter_of(decoded, truncated=len(head) == SNIFF_BYTES) is not None:
        return MediaType.CSV
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        return MediaType.MARKDOWN
    return MediaType.TXT


def detect_delimiter(path: Path) -> str:
    """The delimiter :func:`sniff_media_type` found, or a comma.

    The CSV adapter needs the delimiter, and re-deriving it there would let the
    two disagree — a file could sniff as CSV on semicolons and then be read on
    commas, producing one wide column per row. Sniffing decides once.
    """
    head = path.read_bytes()[:SNIFF_BYTES]
    decoded = head.decode("utf-8", errors="ignore")
    return _delimiter_of(decoded, truncated=len(head) == SNIFF_BYTES) or ","


def _delimiter_of(text: str, *, truncated: bool) -> str | None:
    """The delimiter giving a consistent field count over the sample, if any.

    Consistency is the whole test, and it is what separates a table from prose:
    an English paragraph has commas, but not the *same number* of them on every
    line. Field counting goes through :mod:`csv` rather than ``str.count`` so a
    quoted value containing the delimiter does not derail the count.

    The last line is dropped only when the read actually hit
    :data:`SNIFF_BYTES`, because that is the only time it can be half a row
    whose short field count would look like an inconsistency. Dropping it
    unconditionally would throw away one of the two lines a small file has, and
    a three-line CSV would sniff as plain text.

    **Known limit:** prose laid out with a rigidly repeating comma pattern —
    an address list, say — sniffs as CSV. That is the right call more often
    than not, and the failure is visible rather than silent: the passages come
    out as bare unpaired rows, not as something that looks like a clean table.
    """
    lines = text.splitlines()
    if truncated and len(lines) > 1:
        lines = lines[:-1]
    sample = [line for line in lines if line.strip()][:CSV_SAMPLE_LINES]
    if len(sample) < CSV_MIN_LINES:
        return None

    for delimiter in CSV_DELIMITERS:
        try:
            counts = {
                len(fields)
                for fields in csv.reader(sample, delimiter=delimiter)
                if fields
            }
        except csv.Error:
            continue
        if len(counts) == 1 and counts.pop() >= CSV_MIN_FIELDS:
            return delimiter
    return None
