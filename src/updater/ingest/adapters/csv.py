"""CSV extraction (BUILD_SPEC.md section 3).

One passage per populated row, anchored by :class:`~updater.models.CellLocator`
and serialised by :mod:`updater.ingest.tabular`, as XLSX is, so a table exported
to CSV produces the same passage text as the workbook. ``sheet`` is the file
stem, so a citation reads ``sector-skills-2025!B4:F4``.

The delimiter is sniffed, not assumed: a European export is semicolon-separated,
and reading it on commas gives one enormous column per row.
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
