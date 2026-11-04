"""XLSX extraction (BUILD_SPEC.md section 3).

One passage per populated row, anchored by :class:`~updater.models.CellLocator`
and serialised by :mod:`updater.ingest.tabular`, shared with CSV.

* Every sheet is read, hidden ones included. A hidden sheet is usually
  workings, but "usually" is no basis for silently dropping data.
* Merged values propagate down, never across; see :func:`_merged_value_map`.
* Formulas are read as cached results (``data_only=True``). A workbook saved by
  something other than Excel may have no cache, which shows as empty cells
  rather than as a wrong number.
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
                # A cell with no coordinates has no address to cite. MergedCells
                # inside a range do carry theirs, which is how values from
                # _merged_value_map reach the rows under a vertical merge.
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

    **Merges propagate down, never across.** A merge spanning rows labels each
    of them: ``A2:A4`` holding a provider name describes three data rows, which
    would otherwise come out as unlabelled numbers. A merge spanning *columns*
    is one value (a banner or title); copying ``A1:D1`` into four cells would
    make it look like a data row and pair it against the headers. So only the
    leftmost column of a merge is filled, which is also the address Excel
    reports for it.

    Built once per sheet rather than searched per cell, to avoid a range
    containment test for every cell.
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
