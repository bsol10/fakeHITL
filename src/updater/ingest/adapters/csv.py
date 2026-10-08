"""CSV extraction (BUILD_SPEC.md section 3).

One passage per populated row, anchored by
:class:`~updater.models.CellLocator`, using the same serialisation as the XLSX
adapter — see :mod:`updater.ingest.tabular` for why a row becomes what it does.
Sharing that module is the point: a skills table exported to CSV and the same
table in a workbook should produce the same passage text, or the retrieval index
quietly depends on which button somebody pressed when they saved it.

A CSV has no sheet, so :attr:`~updater.models.CellLocator.sheet` is the file
stem, as the locator's own docstring specifies. That keeps a rendered citation
readable — ``(sector-skills-2025!B4:F4)`` — instead of naming a sheet that does
not exist.

Everything a CSV lacks relative to a workbook is a thing this adapter does not
have to handle: no merges, no formulas, no number formats. What it does have to
handle is the delimiter, which is sniffed rather than assumed, because a
European export is semicolon-separated and reading it with commas produces one
enormous column that would sail through the quality gate as a single passage per
row and be useless to retrieve.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from updater.ingest.errors import ExtractionFailed
from updater.ingest.sniff import detect_delimiter
from updater.ingest.tabular import Table, TableRow, table_to_passages
from updater.models import MediaType, Passage, Source

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["CSV_FIELD_LIMIT", "CsvExtractor"]

CSV_FIELD_LIMIT = 1_000_000
"""Widen Python's default field cap; a pasted note in one cell can exceed it."""


class CsvExtractor:
    """Row-level extraction from delimiter-separated text."""

    media_types: ClassVar[set[str]] = {MediaType.CSV}

    def extract(self, path: Path, source: Source) -> list[Passage]:
        delimiter = detect_delimiter(path)
        text = path.read_text(encoding="utf-8-sig", errors="replace")

        previous_limit = csv.field_size_limit(CSV_FIELD_LIMIT)
        try:
            reader = csv.reader(text.splitlines(), delimiter=delimiter)
            rows = self._read_rows(reader, path=path, source=source)
        finally:
            csv.field_size_limit(previous_limit)

        extracted_at = datetime.now(UTC)
        table = Table(sheet=path.stem, rows=rows)
        passages = table_to_passages(table, source=source, extracted_at=extracted_at)
        if not passages:
            raise ExtractionFailed(
                path=path, source_id=source.id, reason="has no populated rows"
            )
        return passages

    @staticmethod
    def _read_rows(
        reader: Iterable[list[str]],
        *,
        path: Path,
        source: Source,
    ) -> list[TableRow]:
        rows: list[TableRow] = []
        try:
            for number, fields in enumerate(reader, start=1):
                values = {
                    index: field.strip()
                    for index, field in enumerate(fields)
                    if field.strip()
                }
                if values:
                    # Blank lines are skipped without renumbering, so row 7 of
                    # the citation is row 7 of the file a reviewer opens.
                    rows.append(TableRow(number=number, values=values))
        except csv.Error as exc:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason=f"could not be parsed as delimited text: {exc}",
            ) from exc
        return rows
