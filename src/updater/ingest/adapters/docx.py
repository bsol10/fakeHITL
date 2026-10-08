"""DOCX extraction (BUILD_SPEC.md section 3).

Two kinds of passage, interleaved in document order.

**Body paragraphs** get one passage each, anchored by
:class:`~updater.models.BlockLocator`. ``paragraph_index`` counts every
paragraph in the document, including the empty ones this adapter skips. That is
deliberate: the index has to address the real document so a reviewer opening the
file lands on the right block. Renumbering to be dense would make the locator
point at whatever happens to be nearby.

**Tables** get one passage per row, anchored by
:class:`~updater.models.CellLocator`, through the same serialisation the XLSX
adapter uses — :mod:`updater.ingest.tabular`. A table cell is tabular data, and
the header-carrying row serialisation is what makes a bare figure interpretable
once it has been pulled out of the document. A ``BlockLocator`` would throw that
column meaning away and leave a number with a paragraph number attached.

``CellLocator.sheet`` carries the table's identity: ``Table 3 (para 194)``. A
DOCX has no sheets, so the field is being used more loosely than the XLSX
adapter uses it — see the decisions log. The number in brackets is the index the
*next* body paragraph will have, so a reviewer scrolls to that paragraph and the
table is immediately above it. Rendered whole, a citation reads
``(Table 3 (para 194)!B4)``, which is addressable without opening anything else.

Reading tables was not in the first version of this adapter, and their absence
was silent: ``document.paragraphs`` is body-level paragraphs only, so 30% of the
text in ASIL's own reports — the 30% holding the figures — never reached a
passage at all. Nothing raised. That is precisely the silent loss section 3
forbids, which is why tables are read here rather than left to a later phase.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from docx import Document
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from updater.ingest.base import build_passage
from updater.ingest.errors import ExtractionFailed
from updater.ingest.tabular import Table, TableRow, table_to_passages
from updater.models import BlockLocator, MediaType, Passage, Source

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["DocxExtractor"]

_PARAGRAPH_TAG = "p"
_TABLE_TAG = "tbl"


class DocxExtractor:
    """Paragraphs and tables, in the order the document puts them."""

    media_types: ClassVar[set[str]] = {MediaType.DOCX}

    def extract(self, path: Path, source: Source) -> list[Passage]:
        document = Document(str(path))
        extracted_at = datetime.now(UTC)

        passages: list[Passage] = []
        paragraph_index = 0
        table_ordinal = 0

        for element in document.element.body:
            tag = str(element.tag).rsplit("}", maxsplit=1)[-1]

            if tag == _PARAGRAPH_TAG:
                paragraph = Paragraph(element, document)
                passage = self._paragraph_passage(
                    paragraph,
                    index=paragraph_index,
                    source=source,
                    extracted_at=extracted_at,
                )
                # Incremented whether or not the paragraph produced a passage:
                # the index addresses the document, not our output.
                paragraph_index += 1
                if passage is not None:
                    passages.append(passage)

            elif tag == _TABLE_TAG:
                table_ordinal += 1
                rows = list(_table_rows(DocxTable(element, document)))
                if not rows:
                    continue
                # Interleaved, not appended. Where a table sits is part of what
                # it means: the paragraph above it is what says what it shows.
                passages.extend(
                    table_to_passages(
                        Table(
                            sheet=table_name(table_ordinal, paragraph_index),
                            rows=rows,
                        ),
                        source=source,
                        extracted_at=extracted_at,
                    )
                )

        if not passages:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason="has no paragraphs or tables with text in them",
            )
        return passages

    @staticmethod
    def _paragraph_passage(
        paragraph: Paragraph,
        *,
        index: int,
        source: Source,
        extracted_at: datetime,
    ) -> Passage | None:
        text = paragraph.text.strip()
        if not text:
            return None

        style = ""
        if paragraph.style is not None and paragraph.style.name:
            style = paragraph.style.name

        return build_passage(
            source=source,
            locator=BlockLocator(paragraph_index=index, style=style),
            text=text,
            locator_key=f"para{index}",
            extracted_at=extracted_at,
        )


def table_name(ordinal: int, paragraph_index: int) -> str:
    """The identity a table's citations carry: ``Table 3 (para 194)``.

    ``paragraph_index`` is the index the next body paragraph will have, so the
    table sits immediately *above* the paragraph named. A table before any
    paragraph is therefore "(para 0)", which is well defined rather than a
    special case.
    """
    return f"Table {ordinal} (para {paragraph_index})"


def _table_rows(table: DocxTable) -> Iterator[TableRow]:
    """A DOCX table as :class:`~updater.ingest.tabular.TableRow` objects.

    Row numbers are 1-based within the table, so a citation reads ``B4`` and a
    reviewer counts four rows down that table rather than four rows down the
    page. Empty rows are skipped without renumbering, as everywhere else.

    Merged cells need the same asymmetry the XLSX adapter applies, arrived at
    from the other direction. python-docx repeats one underlying ``<w:tc>``
    across every grid column a horizontal merge covers, so a banner row spanning
    a four-column table arrives as four identical strings; keeping only the
    first occurrence turns it back into the single cell it is. Vertical merges
    need nothing: python-docx already reports the merged text on each row the
    merge covers, which is the propagate-down behaviour the XLSX adapter has to
    build by hand.
    """
    for number, row in enumerate(table.rows, start=1):
        values: dict[int, str] = {}
        seen: set[int] = set()
        for column, cell in enumerate(row.cells):
            # cell._tc is private, and it is the only handle python-docx
            # gives on which grid columns share one underlying cell.
            identity = id(cell._tc)
            if identity in seen:
                continue
            seen.add(identity)
            text = " ".join(cell.text.split())
            if text:
                values[column] = text
        if values:
            yield TableRow(number=number, values=values)
