"""Shared fixtures and fixture builders.

Fixture files are generated at test time, never committed as binary blobs.

The ``*_locators`` helpers assert the locator variant before returning it, so a
test that reads ``.page`` cannot pass against the wrong kind of locator.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from docx import Document
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from updater.extract import ModelReply
from updater.models import (
    BlockLocator,
    CellLocator,
    DomLocator,
    MediaType,
    PageLocator,
    Passage,
    Source,
    SourceAnchor,
    UpdateFrequency,
)
from updater.retrieve.store import DB_PATH_ENV_VAR


@pytest.fixture(autouse=True)
def _no_test_touches_the_real_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``UPDATER_DB_PATH`` at a temporary file for every test.

    A command given no ``--db`` opens the configured database, which holds the
    append-only audit log, and a test run cannot tidy that up afterwards.
    """
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(tmp_path / "updater.sqlite3"))


def make_source(
    *,
    source_id: str = "dfe-destinations-2025",
    media_type: str = MediaType.PDF,
) -> Source:
    """A registry entry standing in for a real approved source."""
    return Source(
        id=source_id,
        title="Destinations of KS4 and KS5 students",
        url_or_path="https://example.org/destinations-2025",
        owner="Department for Education",
        topic="post-16 outcomes",
        year=2025,
        update_frequency=UpdateFrequency.ANNUAL,
        media_type=media_type,
    )


# Fixture content

PAGE_ONE = [
    "Post-16 destinations, 2025",
    "Sustained destinations rose to 68% of the 2024 leaver cohort, measured",
    "six months after the end of key stage 4. This continues the upward trend",
    "reported in each of the previous three publications.",
]

PAGE_TWO = [
    "Method",
    "Figures are drawn from matched administrative records covering all",
    "state-funded schools in England. A destination counts as sustained where",
    "the young person remained in the same provision for at least two terms.",
]

HTML_PAGE = """<!DOCTYPE html>
<html lang="en"><head><title>Post-16 destinations 2025</title></head>
<body>
<nav id="site-nav"><a href="/">Home</a> <a href="/about">About us</a></nav>
<main>
<article>
<h1>Post-16 destinations, 2025</h1>
<p>Sustained destinations rose to 68% of the 2024 leaver cohort, measured six
months after the end of key stage 4. This continues the upward trend reported in
each of the previous three publications and is the highest figure recorded since
the series began.</p>
<h2>Method</h2>
<p>Figures are drawn from matched administrative records covering all
state-funded schools in England. A destination is counted as sustained where the
young person remained in the same provision for at least two terms without a
recorded break of more than four weeks.</p>
<h2>Caveats</h2>
<p>Coverage of independent training providers remains incomplete, and the
published figures should be treated as a lower bound for that group in
particular. Comparisons with years before 2019 are not advised.</p>
</article>
</main>
<footer><p>Crown copyright. Contact us at info@example.org.</p></footer>
</body></html>"""

DOCX_BLOCKS: tuple[tuple[str, str], ...] = (
    ("Post-16 destinations, 2025", "Heading 1"),
    (
        "Sustained destinations rose to 68% of the 2024 leaver cohort, measured "
        "six months after the end of key stage 4.",
        "",
    ),
    ("", ""),  # index 2: empty on purpose, to prove indices are not renumbered
    (
        "Figures are drawn from matched administrative records covering all "
        "state-funded schools in England.",
        "",
    ),
)

BANNERED_TABLE: tuple[tuple[str, ...], ...] = (
    # Modelled on the automation-risk table in ASIL_Jobs_of_the_Future.docx: one
    # header, then banner rows subdividing the data. The banners cut the header
    # off from its rows, so without inheritance the first data row is crowned.
    ("Industry", "Risk Group", "Est. Automation Risk", "Role Type"),
    ("Higher automation risk industries (9 industries)",),
    ("Manufacturing", "Higher risk", "~74%", "Routine physical production"),
    ("Agriculture, Forestry & Fishing", "Higher risk", "~62%", "Seasonal manual work"),
    ("Mining", "Higher risk", "~55%", "Resource extraction"),
    ("Lower automation risk industries (10 industries)",),
    ("Health Care & Social Assistance", "Lower risk", "~14%", "Human care"),
    ("Education & Training", "Lower risk", "~8%", "Teaching"),
)

NUMERIC_BANNERED_TABLE: tuple[tuple[str, ...], ...] = (
    # Table 2 of ASIL_Jobs_of_the_Future.docx. Same banner shape as
    # BANNERED_TABLE, but every data column is a measurement, so the first row
    # of an orphaned block passes every structural test for a header. Only kind
    # tells them apart: "Higher Risk Industries (avg %)" is a label, "54.4%" is
    # not.
    ("Enterprise Skill", "Higher Risk Industries (avg %)", "Difference"),
    ("Skills significantly more demanded in lower-risk industries",),
    ("Communication", "54.4%", "+31.0pp"),
    ("Creativity", "2.0%", "+10.4pp"),
    ("Skills broadly similar across both groups",),
    ("Leadership", "37.1%", "+3.3pp"),
    ("Presentation", "2.0%", "-2.0pp"),
)


# Fixture writers


def write_pdf(path: Path, pages: Sequence[Sequence[str]]) -> Path:
    """Write a PDF with a real text layer, one page per element of ``pages``."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        y = 800.0
        for line in lines:
            pdf.drawString(60, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return path


def write_scanned_pdf(path: Path, page_count: int = 3) -> Path:
    """Write a PDF whose text layer is only page numbers, as a scan looks to pypdf."""
    return write_pdf(path, [[str(number)] for number in range(1, page_count + 1)])


def write_docx(path: Path, blocks: Sequence[tuple[str, str]] = DOCX_BLOCKS) -> Path:
    """Write a DOCX, one paragraph per ``(text, style_name)`` pair."""
    document = Document()
    for text, style in blocks:
        document.add_paragraph(text, style=style or None)
    document.save(str(path))
    return path


def write_docx_with_table(
    path: Path,
    *,
    before: Sequence[str] = ("Automation Risk and Enterprise Skill Demand",),
    table: Sequence[Sequence[str]] = BANNERED_TABLE,
    after: Sequence[str] = ("Source: OECD (2016).",),
    banner_rows: Sequence[int] = (2, 6),
) -> Path:
    """A DOCX with paragraphs, then a table, then more paragraphs.

    ``banner_rows`` are 1-based row numbers to merge across the full width, the
    way a real ASIL table marks a subheading inside itself.
    """
    document = Document()
    for text in before:
        document.add_paragraph(text)

    width = max(len(row) for row in table)
    docx_table = document.add_table(rows=len(table), cols=width)
    for row_number, row in enumerate(table):
        cells = docx_table.rows[row_number].cells
        if row_number + 1 in banner_rows:
            merged = cells[0]
            for cell in cells[1:]:
                merged = merged.merge(cell)
            merged.text = row[0]
            continue
        for column, value in enumerate(row):
            cells[column].text = value

    for text in after:
        document.add_paragraph(text)
    document.save(str(path))
    return path


def write_html(path: Path, html: str = HTML_PAGE) -> Path:
    path.write_text(html, encoding="utf-8")
    return path


def write_xlsx(
    path: Path,
    sheets: Mapping[str, Sequence[Sequence[object]]],
    *,
    merges: Mapping[str, Sequence[str]] | None = None,
    percent_cells: Mapping[str, Sequence[str]] | None = None,
) -> Path:
    """Write a workbook from ``{sheet name: rows}``.

    A row is a sequence of cell values; ``None`` leaves the cell empty and an
    empty sequence leaves the whole row blank. ``merges`` and ``percent_cells``
    take A1 ranges and coordinates per sheet.
    """
    workbook = Workbook()
    default_sheet = workbook.active
    if default_sheet is not None:
        workbook.remove(default_sheet)

    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        for row_number, row in enumerate(rows, start=1):
            for column_number, value in enumerate(row, start=1):
                if value is None:
                    continue
                worksheet.cell(row=row_number, column=column_number, value=value)
        for merge in (merges or {}).get(name, ()):
            worksheet.merge_cells(merge)
        for coordinate in (percent_cells or {}).get(name, ()):
            worksheet[coordinate].number_format = "0.0%"

    workbook.save(str(path))
    return path


def write_skills_workbook(path: Path) -> Path:
    """The standard two-sheet fixture: preamble, late header, merged title."""
    return write_xlsx(
        path,
        {"Apprenticeship starts": STARTS_SHEET, "Skills gaps": GAPS_SHEET},
        merges={"Apprenticeship starts": ("A1:D1",)},
        percent_cells={"Apprenticeship starts": ("D6", "D7", "D9", "D10")},
    )


def write_csv(
    path: Path, rows: Sequence[Sequence[object]], *, delimiter: str = ","
) -> Path:
    """Write delimited text, ``None`` becoming an empty field."""
    lines = []
    for row in rows:
        fields = ["" if value is None else str(value) for value in row]
        lines.append(delimiter.join(fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# Tabular fixture content

STARTS_SHEET: tuple[tuple[object, ...], ...] = (
    # Row 1: a title merged to the table's full width, so a naive "widest row"
    # header search would pick it.
    ("Table 3: Apprenticeship starts by region", None, None, None),
    (),  # row 2 blank
    ("Source: Department for Education, published March 2025", None, None, None),
    (),  # row 4 blank
    # Row 5 is the header. Two of its four labels are years, so anything that
    # looks for "the row of text above the row of numbers" fails here.
    ("Region", "Starts 2023", "Starts 2024", "Change on year"),
    ("North East", 12340, 11845, -0.04),
    ("North West", 31200, 32015, 0.026),
    (),  # row 8 blank, mid-table
    ("Yorkshire and the Humber", 24870, 24110, -0.031),
    ("London", 41005, 43220, 0.054),
    (),
    ("Figures for 2024 are provisional.", None, None, None),
)
"""Preamble, a header at row 5, a blank row mid-table, and a trailing note."""

GAPS_SHEET: tuple[tuple[object, ...], ...] = (
    ("Occupation", "SOC code", "Vacancies", "Density"),
    ("Care worker", 6135, 1204, 0.34),
    ("Teaching assistant", 6125, 388, 0.11),
)
"""A second sheet, header at row 1, to prove sheet names are not shared."""

MATRIX_SHEET: tuple[tuple[object, ...], ...] = (
    # From FYA_vs_ASIL_Comparative_Analysis.xlsx, sheet "Overview". Column A
    # holds row labels, so the header's corner cell is empty: the header is
    # narrower than every row under it, but ends at the same column.
    ("Study Comparison",),
    (None, "FYA New Basics (2016)", "ASIL Research (2026)"),
    ("Data source", "4.2M job ads", "950 job ads"),
    ("Reference period", "2012 to 2015", "2025 to 2026"),
    ("Industry coverage", "All 19 ANZSIC divisions", "All 19 ANZSIC divisions"),
)

TWO_TABLE_SHEET: tuple[tuple[object, ...], ...] = (
    # From the "Salary Premiums" sheet of the ASIL comparative workbook: two
    # tables under one tab, each with its own header. Applying the first header
    # to the second table cites $109,860, an average salary, as a pay premium.
    ("FYA (2016): Pay Premium for Enterprise Skills",),
    ("Enterprise Skill", "Pay Premium", "Context"),
    ("Presentation", "$8,853", "Highest premium skill"),
    ("Creativity", "$3,129", "Steady premium in 2016"),
    (),
    ("ASIL (2026): Average Advertised Salary by Industry",),
    ("Industry Division", "Avg Salary Low", "Avg Salary High"),
    ("Professional, Scientific & Technical", "$109,860", "$160,000"),
    ("Construction", "$90,000", "$150,000"),
)

MERGED_SHEET: tuple[tuple[object, ...], ...] = (
    ("Provider type", "Year", "Learners"),
    ("Further education college", 2023, 8800),
    (None, 2024, 9150),  # A3 is blank: it is merged up into A2
    (None, 2025, 9410),  # A4 likewise
)
"""A vertical merge over A2:A4 — the shape that loses labels if not propagated."""


SENTINEL_MARKERS = ("ALPHAPAGE", "BRAVOPAGE", "CHARLIEPAGE")


def write_sentinel_pdf(path: Path) -> Path:
    """A three-page PDF whose pages differ only by one unique marker each.

    A test can then assert that a page's passage contains its own marker and
    *neither* of the others: positive containment alone would not catch a reader
    that leaked adjacent pages into the text.
    """
    pages = [
        [
            f"{marker} is the only marker on this page.",
            "Sustained destinations rose to 68% of the 2024 leaver cohort,",
            "measured six months after the end of key stage 4.",
        ]
        for marker in SENTINEL_MARKERS
    ]
    return write_pdf(path, pages)


# Locator assertions


def page_locators(passages: Sequence[Passage]) -> list[PageLocator]:
    """Assert every passage is page-anchored, and return the locators."""
    locators = []
    for passage in passages:
        locator = passage.anchor.locator
        assert isinstance(locator, PageLocator), (
            f"{passage.id} carries {type(locator).__name__}, expected PageLocator"
        )
        locators.append(locator)
    return locators


def block_locators(passages: Sequence[Passage]) -> list[BlockLocator]:
    """Assert every passage is block-anchored, and return the locators."""
    locators = []
    for passage in passages:
        locator = passage.anchor.locator
        assert isinstance(locator, BlockLocator), (
            f"{passage.id} carries {type(locator).__name__}, expected BlockLocator"
        )
        locators.append(locator)
    return locators


def dom_locators(passages: Sequence[Passage]) -> list[DomLocator]:
    """Assert every passage is DOM-anchored, and return the locators."""
    locators = []
    for passage in passages:
        locator = passage.anchor.locator
        assert isinstance(locator, DomLocator), (
            f"{passage.id} carries {type(locator).__name__}, expected DomLocator"
        )
        locators.append(locator)
    return locators


def cell_locators(passages: Sequence[Passage]) -> list[CellLocator]:
    """Assert every passage is cell-anchored, and return the locators."""
    locators = []
    for passage in passages:
        locator = passage.anchor.locator
        assert isinstance(locator, CellLocator), (
            f"{passage.id} carries {type(locator).__name__}, expected CellLocator"
        )
        locators.append(locator)
    return locators


# Chunking, embedding and retrieval helpers


@dataclass(frozen=True, slots=True)
class WordCounter:
    """Counts whitespace-delimited words.

    Stands in for the word-piece counter in tests about *boundaries* rather than
    tokenization: "chunk has 7 words" can be checked by eye in a failure
    message, "chunk has 7 word pieces" cannot.
    """

    def count(self, text: str) -> int:
        return len(text.split())


class StubEmbedder:
    """Deterministic unit vectors hashed from the text, with no model or torch.

    Identical strings embed identically and different strings almost always
    differ, which is all the store and the retriever need.
    """

    dimensions = 8

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [digest[index] - 127.5 for index in range(self.dimensions)]
            norm = math.sqrt(sum(value * value for value in raw)) or 1.0
            vectors.append([value / norm for value in raw])
        return vectors


MARKER_COUNT = 40


def marker(index: int) -> str:
    """A unique token that appears exactly once in :func:`sentinel_text`.

    Fixed width so no marker is a substring of another: otherwise MARKER1 would
    be found inside MARKER10.
    """
    return f"MARKER{index:02d}"


def sentinel_text(count: int = MARKER_COUNT, filler_words: int = 12) -> str:
    """Text whose every region is identifiable by a unique marker.

    A chunk can then be asked to prove the *absence* of everything outside its
    own span; positive containment alone would pass a chunker that leaked
    adjacent text into a chunk.
    """
    filler = " ".join(["destinations"] * filler_words)
    return "\n\n".join(f"{marker(index)} {filler}." for index in range(count))


def make_passage(
    text: str,
    *,
    passage_id: str = "src:page7",
    source_id: str = "src",
    locator: object | None = None,
    char_start: int = 0,
    char_end: int | None = None,
) -> Passage:
    """A passage anchored to one locator unit, spanning all of its text."""
    anchor = SourceAnchor(
        source_id=source_id,
        locator=locator or PageLocator(page=7),  # type: ignore[arg-type]
        char_start=char_start,
        char_end=len(text) if char_end is None else char_end,
        extracted_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )
    return Passage(
        id=passage_id,
        source_id=source_id,
        text=text,
        anchor=anchor,
        token_count=len(text) // 4,
    )


def assert_chunk_slices_back(parent: Passage, chunk: Passage) -> None:
    """Slicing the parent's text with the chunk's anchor offsets returns the chunk.

    If it does not, the chunk's citation points at text the chunk does not
    contain.
    """
    offset = parent.anchor.char_start
    start = chunk.anchor.char_start - offset
    end = chunk.anchor.char_end - offset
    assert parent.text[start:end] == chunk.text, (
        f"{chunk.id} says it covers [{chunk.anchor.char_start}, "
        f"{chunk.anchor.char_end}) but the parent's text there is "
        f"{parent.text[start:end]!r}, not {chunk.text!r}"
    )


def write_registry(path: Path, entries: Sequence[Mapping[str, object]]) -> Path:
    """Write a registry YAML file from a list of entry mappings."""
    path.write_text(yaml.safe_dump({"sources": list(entries)}), encoding="utf-8")
    return path


def registry_entry(**overrides: object) -> dict[str, object]:
    """A complete, valid registry entry, with fields overridable per test."""
    entry: dict[str, object] = {
        "id": "dfe-destinations-2025",
        "title": "Destinations of KS4 and KS5 students",
        "url_or_path": "destinations-2025.pdf",
        "owner": "Department for Education",
        "topic": "post-16 outcomes",
        "year": 2025,
        "update_frequency": "annual",
        "media_type": "application/pdf",
    }
    entry.update(overrides)
    return entry


# Evidence extraction

SCRIPTED_ANSWERED_BY = "scripted-model-2026-10-08"
"""What the scripted model's "API" reports, deliberately not the id it was
configured with, so a test can tell which of the two a result recorded."""


@dataclass
class ScriptedModel:
    """An :class:`~updater.extract.EvidenceModel` that replies with fixed text
    and records every request, so extraction is testable with no API key."""

    reply: str
    provider: str = "scripted"
    model: str = "scripted-model"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any]
    ) -> ModelReply:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        return ModelReply(
            text=self.reply, model=SCRIPTED_ANSWERED_BY, provider=self.provider
        )


def finding(**overrides: object) -> dict[str, object]:
    """One finding as a model returns it: every field present, nullables null."""
    item: dict[str, object] = {
        "passage_id": "src:page7",
        "quote": "",
        "type": "statistic",
        "finding": "A finding.",
        "statistic": None,
        "method": None,
        "as_of_date": None,
        "caveat": None,
    }
    item.update(overrides)
    return item


def evidence_reply(*items: object) -> str:
    """A model reply holding these findings, as JSON text."""
    return json.dumps({"evidence": list(items)})
