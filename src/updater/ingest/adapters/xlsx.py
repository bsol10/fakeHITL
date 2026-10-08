"""XLSX extraction (BUILD_SPEC.md section 3).

One passage per populated row, anchored by
:class:`~updater.models.CellLocator`. The serialisation — and the reasoning
behind one-passage-per-row — lives in :mod:`updater.ingest.tabular`, which the
CSV adapter shares so the two formats cannot drift.

This module owns the three things that are specific to real workbooks:

*Every sheet is read, hidden ones included.* A hidden sheet is usually workings,
but "usually" is not a basis for dropping data silently, and silent loss is what
BUILD_SPEC.md section 3 rules out. A hidden sheet that contributes nothing is a
few empty rows; a hidden sheet that held the figures and was skipped is a
missing finding.

*Merged cells are propagated to every cell in the merge.* openpyxl puts the
value in the top-left cell of a merged range and leaves the rest ``None``.
Reading them raw loses a region name merged down three rows — the second and
third rows come out unlabelled and their passages become uncitable numbers.
Propagating gives every row what a human sees when they look at the sheet.

*Formulas are read as their cached results*, via ``data_only=True``. A passage
saying ``=SUM(B2:B14)`` is not evidence about the world. The tradeoff is that a
workbook saved by something other than Excel may have no cached values, which
surfaces as empty cells rather than as a wrong number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from updater.ingest.errors import ExtractionFailed
from updater.ingest.tabular import Table, TableRow, format_value, table_to_passages
from updater.models import MediaType, Passage, Source

if TYPE_CHECKING:
    from openpyxl.worksheet.worksheet import Worksheet

__all__ = ["XlsxExtractor"]


class XlsxExtractor:
    """Row-level extraction across every sheet in the workbook."""

    media_types: ClassVar[set[str]] = {MediaType.XLSX}

    def extract(self, path: Path, source: Source) -> list[Passage]:
        try:
            workbook = load_workbook(str(path), data_only=True, read_only=False)
        except (InvalidFileException, KeyError, OSError) as exc:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason=f"could not be parsed as a workbook: {exc}",
            ) from exc

        extracted_at = datetime.now(UTC)
        passages: list[Passage] = []
        try:
            for worksheet in workbook.worksheets:
                table = self._read_sheet(worksheet)
                passages.extend(
                    table_to_passages(table, source=source, extracted_at=extracted_at)
                )
        finally:
            workbook.close()

        if not passages:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason="has no sheet with any populated rows",
            )
        return passages

    @staticmethod
    def _read_sheet(worksheet: Worksheet) -> Table:
        """One sheet as populated rows, with merged values filled in."""
        merged = _merged_value_map(worksheet)

        rows: list[TableRow] = []
        for row_cells in worksheet.iter_rows():
            values: dict[int, str] = {}
            row_number: int | None = None
            for cell in row_cells:
                # A MergedCell reports no coordinates of its own. Skipping it is
                # not a loss: _merged_value_map has already put the range's
                # value on the anchor cell, which does have coordinates.
                if cell.row is None or cell.column is None:
                    continue
                row_number = cell.row
                raw = merged.get((cell.row, cell.column), cell.value)
                number_format = str(cell.number_format or "")
                text = format_value(raw, number_format=number_format).strip()
                if text:
                    # cell.column is 1-indexed; TableRow columns are 0-indexed.
                    values[cell.column - 1] = text
            if values and row_number is not None:
                # Blank rows are skipped and the numbering is left alone, so a
                # citation still names the row the workbook itself shows.
                rows.append(TableRow(number=row_number, values=values))

        return Table(sheet=str(worksheet.title), rows=rows)


def _merged_value_map(worksheet: Worksheet) -> dict[tuple[int, int], object]:
    """Cells inside a merged range mapped to that range's value.

    **Merges are propagated down, never across**, and the asymmetry is the whole
    point. A merge spanning rows means "this label applies to each of these
    rows": ``A2:A4`` holding "Further education college" describes three data
    rows, and without propagation the second and third come out unlabelled, as
    bare numbers no reviewer could check. Filling them in is what a human sees.

    A merge spanning *columns* is one value, not several — a banner or a table
    title. Copying ``A1:D1`` into four cells would manufacture four cells where
    the sheet has one, and a title as wide as the table then looks exactly like
    a data row: it gets paired against the headers and produces
    ``Region: Table 3; Starts 2023: Table 3``, which is nonsense with a
    perfectly good anchor on it. So a horizontal merge keeps only its top-left
    cell, which is also the address Excel itself reports for it.

    Built once per sheet rather than searched per cell: a workbook with a few
    hundred merges and a few thousand rows would otherwise spend all its time in
    range containment tests.
    """
    filled: dict[tuple[int, int], object] = {}
    for merged_range in worksheet.merged_cells.ranges:
        min_col, min_row, _max_col, max_row = merged_range.bounds
        anchor_value = worksheet.cell(row=min_row, column=min_col).value
        if anchor_value is None:
            continue
        for row in range(min_row, max_row + 1):
            filled[(row, min_col)] = anchor_value
    return filled
