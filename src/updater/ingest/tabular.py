"""Turning a grid of cells into passages retrieval can match (BUILD_SPEC.md §3).

Shared by the XLSX, CSV and DOCX adapters: the parsing differs, but the same
table must produce the same passage text whichever library read it.

**One passage per row**, anchored at its cells (``Sheet2!B4:F4``). A single cell
is a bare number nothing can retrieve; a whole sheet is an anchor no reviewer
can check (see DESIGN.md decisions log).

**Each value is written next to its column header**, because a bare row
(``North East; 12340; 11845``) embeds close to nothing::

    Apprenticeship starts by region. Region: North East; Starts 2023: 12,340

The sheet name leads, because it is part of the address the locator already
names and often the only place the table's subject is written. Title and note
rows are *not* spliced into data rows; they become passages of their own.
Copying ``A1`` into a passage anchored at ``B47:F47`` would make the text stop
matching the cells it cites (rule 1). A header is different in kind: it is part
of what the cell *means*.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from openpyxl.utils import get_column_letter

from updater.ingest.base import build_passage
from updater.models import CellLocator, Passage, Source

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "HEADER_SEARCH_ROWS",
    "MIN_PAIRED_CELLS",
    "Table",
    "TableRow",
    "cell_range",
    "collapse_whitespace",
    "column_label",
    "find_header_row",
    "format_value",
    "header_labels",
    "serialise_row",
    "split_tables",
    "table_columns",
    "table_to_passages",
]

HEADER_SEARCH_ROWS = 25
"""How far down to look for the header. Beyond this a sheet has no header."""

MIN_PAIRED_CELLS = 2
"""Populated cells a row needs before its values are paired with headers.

A one-cell row is far more often a footnote ("Source: DfE, 2025") than data,
and pairing it yields ``Region: Source: DfE, 2025``. Such rows are written as
bare text; a genuine one-value row losing its column name is the milder failure.
Also the width below which a row separates tables (:func:`split_tables`).
"""


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row, keyed by 0-indexed column, holding only populated cells.

    ``number`` is the 1-indexed row number the source shows (within the table,
    for DOCX), never renumbered around blank rows, so a reviewer opening the
    file lands on the cited row.
    """

    number: int
    values: dict[int, str]

    @property
    def columns(self) -> list[int]:
        return sorted(self.values)

    @property
    def populated(self) -> int:
        return len(self.values)

    @property
    def span(self) -> list[int]:
        """Every column from the leftmost populated one to the rightmost.

        Unlike :attr:`columns`, includes blank cells in between.
        """
        if not self.values:
            return []
        return list(range(min(self.values), max(self.values) + 1))


@dataclass(frozen=True, slots=True)
class Table:
    """One grid's populated rows, in file order: a sheet, a CSV or a DOCX table."""

    sheet: str
    rows: list[TableRow]


def column_label(index: int) -> str:
    """0-indexed column number to its spreadsheet letter (0 -> A, 26 -> AA)."""
    return str(get_column_letter(index + 1))


def format_value(value: object, *, number_format: str = "") -> str:
    """Render one cell as its stored value, not as the sheet displays it.

    Applying Excel's display format can silently change a number (12.344 shown
    to two places would be cited as 12.34). The one exception is a per-cent
    format, which Excel defines as the stored value times 100: a cell holding
    0.04 and showing "4%" is written ``4%``, or the citation would look wrong to
    the reviewer.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int, and TRUE is a label.
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        # Midnight almost always means a date cell, not an instant.
        if value.hour == value.minute == value.second == 0:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return _apply_percent(float(value), number_format) or str(value)
    if isinstance(value, float):
        percent = _apply_percent(value, number_format)
        if percent:
            return percent
        # repr of a float is the shortest string that round trips, so this
        # neither invents precision nor loses any.
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _apply_percent(value: float, number_format: str) -> str:
    """``0.04`` under a per-cent format becomes ``4%``. Otherwise ``""``."""
    if "%" not in number_format:
        return ""
    scaled = value * 100
    rendered = str(int(scaled)) if float(scaled).is_integer() else f"{scaled:.10g}"
    return f"{rendered}%"


def find_header_row(rows: Sequence[TableRow]) -> TableRow | None:
    """The row whose cells name the columns, or ``None`` if there isn't one.

    Real workbooks open with titles, notes and blanks, and year columns are
    numeric, so neither "the first row" nor "the first row of text" works. The
    header is the first row within :data:`HEADER_SEARCH_ROWS` that:

    - reaches the most common *right edge*. Not width: a comparison matrix's
      header has an empty top-left corner over a column of row labels, so it is
      narrower than its rows but ends at the same column;
    - has all-distinct values, so a title repeated across columns does not win;
    - is not entirely numeric, so a headerless table of figures does not caption
      row 2 with row 1 (``12340: 31200``);
    - does not repeat its own values down its column. A real header names a
      column once; this tells it apart from the first data row of a block cut
      off from its header by a banner (see :func:`table_to_passages`).

    ``None`` is a real answer: the caller falls back to column letters rather
    than inventing names (rule 2). Every condition fixes a failure seen on real
    files; see DESIGN.md decisions log before relaxing one.
    """
    candidates = [row for row in rows[:HEADER_SEARCH_ROWS] if row.populated >= 2]
    if not candidates:
        return None

    edge = _modal_right_edge(candidates)
    for row in candidates:
        if edge is None or max(row.values) != edge:
            continue
        values = [text.strip() for text in row.values.values()]
        if len({value.casefold() for value in values}) != len(values):
            continue
        if all(_is_numeric(value) for value in values):
            continue
        if _repeats_down_a_column(row, candidates):
            continue
        return row
    return None


def _repeats_down_a_column(candidate: TableRow, rows: Sequence[TableRow]) -> bool:
    """Whether any of ``candidate``'s values appears again in its own column."""
    for column, value in candidate.values.items():
        wanted = value.strip().casefold()
        for other in rows:
            if other is candidate:
                continue
            if other.values.get(column, "").strip().casefold() == wanted:
                return True
    return False


def _modal_right_edge(rows: Sequence[TableRow]) -> int | None:
    """The rightmost column most rows of the table reach.

    Counter breaks ties by first appearance, i.e. row order, so a tie goes to
    the earlier shape: the header, since its data comes after it.
    """
    edges = [max(row.values) for row in rows if row.populated >= 2]
    if not edges:
        return None
    return Counter(edges).most_common(1)[0][0]


def table_columns(rows: Sequence[TableRow], header: TableRow | None) -> set[int]:
    """The contiguous column footprint of the table body.

    Wider than the header's own columns when the header has an empty top-left
    corner (B to C over rows spanning A to C); pairing against the header alone
    would leave every row unpaired. The footprint runs from the leftmost column
    any full-width row uses to the right edge, so column A gets its letter as a
    label. Computed over every row, not just the first :data:`HEADER_SEARCH_ROWS`.
    """
    edge = _modal_right_edge(rows)
    if edge is None:
        return set()
    body = [row for row in rows if row.populated >= 2 and max(row.values) == edge]
    if not body:
        return set()
    left = min(min(row.values) for row in body)
    if header is not None:
        left = min(left, min(header.values))
    return set(range(left, edge + 1))


_MEASURE_STRIP = str.maketrans("", "", ",%$£€~+")


def _is_numeric(text: str) -> bool:
    """Whether a cell reads as a measurement rather than a label.

    Generous about decoration: ``54.4%``, ``$109,860``, ``~74%`` and
    ``+31.0pp`` are measurements, while ``Higher Risk Industries (avg %)`` is a
    label.
    """
    candidate = text.strip().removesuffix("pp").translate(_MEASURE_STRIP).strip()
    if not candidate:
        return False
    try:
        float(candidate)
    except ValueError:
        return False
    return True


def introduces_new_header(candidate: TableRow, block: Sequence[TableRow]) -> bool:
    """Whether ``candidate`` reads as a header *for the rows beneath it*.

    True when, in at least one column, the candidate is a label while most rows
    below it are measurements. ``Avg Salary Low (AUD)`` over ``$109,860``
    passes; ``Leadership | 37.1% | 40.4%`` over more percentages does not, being
    the first row of a table whose header is further up.

    Used only to decide whether a block that *could* inherit a header should
    declare its own instead (see :func:`table_to_passages`).
    """
    others = [row for row in block if row is not candidate]
    if not others:
        return False

    for column, value in candidate.values.items():
        if _is_numeric(value):
            continue
        below = [
            row.values[column] for row in others if row.values.get(column, "").strip()
        ]
        if below and sum(_is_numeric(text) for text in below) * 2 >= len(below):
            return True
    return False


def header_labels(header: TableRow | None, columns: Iterable[int]) -> dict[int, str]:
    """Column index to the label used for it, falling back to its letter.

    An unnamed column is called "Column D", never given a plausible name
    (rule 2).
    """
    labels: dict[int, str] = {}
    for index in columns:
        label = header.values.get(index, "").strip() if header else ""
        labels[index] = label or f"Column {column_label(index)}"
    return labels


def cell_range(row_number: int, columns: Sequence[int]) -> str:
    """A1 notation for the populated span of one row: ``B4:F4``, or ``B4``.

    From the first populated column to the last, so a reviewer can paste it
    into Excel's name box. A single cell uses the short form, not ``B4:B4``.
    """
    if not columns:
        raise ValueError(f"row {row_number} has no populated columns to address")
    first = f"{column_label(columns[0])}{row_number}"
    if len(columns) == 1:
        return first
    return f"{first}:{column_label(columns[-1])}{row_number}"


def collapse_whitespace(text: str) -> str:
    """Runs of whitespace become one space.

    Workbook headers contain hard line breaks, which would make a quoted label
    read as two lines. Applied here in serialisation, not in an adapter, so
    XLSX and CSV produce identical text for the same table. The DOCX adapter
    also collapses at parse time; applying it twice is harmless.
    """
    return " ".join(text.split())


def serialise_row(
    *,
    sheet: str,
    row: TableRow,
    labels: Mapping[int, str],
    paired: bool,
) -> str:
    """One row as the text that goes into the index.

    ``paired`` rows are written ``Header: value; Header: value``. Unpaired rows
    (titles, notes, anything a header cannot be trusted against) are written as
    bare values: a label guessed onto a footnote is worse than none.

    Empty cells contribute nothing: no ``Header: —``, no "N/A" (rule 2).
    """
    parts: list[str] = []
    for index in row.columns:
        value = collapse_whitespace(row.values[index])
        if not value:
            continue
        parts.append(
            f"{collapse_whitespace(labels[index])}: {value}" if paired else value
        )
    body = "; ".join(parts)
    return f"{sheet}. {body}" if sheet else body


def split_tables(rows: Sequence[TableRow]) -> list[list[TableRow]]:
    """One sheet's rows cut into blocks, one per table on the sheet.

    A sheet can stack several tables, each with its own header. Treating it as
    one table applies the first header to the second table's rows: a sound
    anchor on the wrong column name, which nothing downstream can detect.

    The cut is made at rows narrower than :data:`MIN_PAIRED_CELLS` (a banner,
    title or note). Each such row stays in the output as a block of its own, so
    nothing is dropped.
    """
    blocks: list[list[TableRow]] = []
    current: list[TableRow] = []
    for row in rows:
        if row.populated < MIN_PAIRED_CELLS:
            if current:
                blocks.append(current)
                current = []
            blocks.append([row])
            continue
        current.append(row)
    if current:
        blocks.append(current)
    return blocks


def table_to_passages(
    table: Table,
    *,
    source: Source,
    extracted_at: datetime,
) -> list[Passage]:
    """Every populated row of one table, as anchored passages.

    Header detection runs per block (see :func:`split_tables`). A block with no
    header of its own **inherits the last one seen**, if they share a right
    edge: banner rows can subdivide one table, and without inheritance the rows
    under a banner lose their labels. A block that could inherit declares its
    own header only when :func:`introduces_new_header` agrees, as when two
    stacked tables each have one; otherwise the first data row of an orphaned
    block would caption the rest.
    """
    passages: list[Passage] = []
    carried_columns: set[int] = set()
    carried_labels: Mapping[int, str] = {}

    for block in split_tables(table.rows):
        candidate = find_header_row(block)
        inheritable = _fits(block, carried_columns)
        header = (
            candidate
            if candidate is not None
            and (not inheritable or introduces_new_header(candidate, block))
            else None
        )
        if header is not None:
            # The table's footprint, not the header's own cells, so a blank
            # label or an empty top-left corner gets its column letter.
            header_columns = table_columns(block, header)
            labels: Mapping[int, str] = header_labels(header, sorted(header_columns))
            carried_columns, carried_labels = header_columns, labels
        elif inheritable:
            header_columns, labels = carried_columns, carried_labels
        else:
            header_columns, labels = set(), {}

        passages.extend(
            _block_passages(
                block,
                sheet=table.sheet,
                header=header,
                header_columns=header_columns,
                labels=labels,
                source=source,
                extracted_at=extracted_at,
            )
        )
    return passages


def _fits(block: Sequence[TableRow], columns: set[int]) -> bool:
    """Whether a headerless block is shaped like the header it would inherit.

    Same right edge, not merely overlapping columns: a narrower table further
    down is a different table, and borrowing its labels would mislabel every row.
    """
    if not columns:
        return False
    edge = _modal_right_edge(block)
    return edge is not None and edge == max(columns)


def _block_passages(
    block: Sequence[TableRow],
    *,
    sheet: str,
    header: TableRow | None,
    header_columns: set[int],
    labels: Mapping[int, str],
    source: Source,
    extracted_at: datetime,
) -> list[Passage]:
    passages: list[Passage] = []
    for row in block:
        columns = row.columns
        if not columns:
            continue

        # Pair only where the header covers the row: a footnote in a column the
        # table never used has no column name. ``labels`` may be inherited, so
        # test the labels, not whether this block found a header.
        paired = (
            bool(labels)
            and row is not header
            and row.populated >= MIN_PAIRED_CELLS
            and set(columns) <= header_columns
        )
        row_labels = labels if paired else {}
        text = serialise_row(sheet=sheet, row=row, labels=row_labels, paired=paired)
        if not text.strip():
            continue

        span = cell_range(row.number, columns)
        passages.append(
            build_passage(
                source=source,
                locator=CellLocator(sheet=sheet, cell_range=span),
                text=text,
                locator_key=f"{sheet}!{span}",
                extracted_at=extracted_at,
            )
        )
    return passages
