"""DOCX extraction (BUILD_SPEC.md section 3).

Two kinds of passage, interleaved in document order.

**Body paragraphs** get one passage each, anchored by
:class:`~updater.models.BlockLocator`. ``paragraph_index`` counts every
paragraph, including the empty ones skipped here, so it addresses the real
document and a reviewer lands on the right block.

**Tables** get one passage per row, anchored by
:class:`~updater.models.CellLocator` and serialised by
:mod:`updater.ingest.tabular`, so each figure carries its column header; a
``BlockLocator`` would lose the column. ``CellLocator.sheet`` holds the table's
identity (see :func:`table_name`), so a citation reads
``Table 3 (para 194)!B4``.

Tables must be walked explicitly: ``document.paragraphs`` excludes text inside
tables, and skipping them silently loses most of a report's figures.
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
                # Interleaved, not appended: the paragraph above a table is what
                # says what it shows.
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
    table sits immediately *above* the paragraph named; a table before any
    paragraph is "(para 0)".
    """
    return f"Table {ordinal} (para {paragraph_index})"


def _table_rows(table: DocxTable) -> Iterator[TableRow]:
    """A DOCX table as :class:`~updater.ingest.tabular.TableRow` objects.

    Row numbers are 1-based within the table, so ``B4`` means four rows down
    that table. Empty rows are skipped without renumbering.

    Merges follow the XLSX rule, down but never across. python-docx repeats one
    ``<w:tc>`` across every grid column a horizontal merge covers, so only its
    first occurrence is kept: a banner is one cell, not four. Vertical merges
    already report their text on every row they cover.
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
