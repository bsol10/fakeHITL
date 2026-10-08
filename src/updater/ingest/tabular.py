"""Turning a grid of cells into passages retrieval can match (BUILD_SPEC.md §3).

Shared by the XLSX and CSV adapters, because the parsing differs and the
serialisation must not: two spreadsheets that say the same thing should produce
the same passage text whichever library read them.

The design question is not parsing. It is *what a row becomes*, and there are
three plausible answers:

**One passage per cell.** Rejected. A lone ``11845`` anchored at ``B7`` is not
evidence, it is a number. Nothing can retrieve it and no reviewer can check it.

**One passage per sheet.** Rejected, and for the same reason a chunk may not
span two PDF pages: an anchor naming a whole sheet is unfalsifiable. A reviewer
told "Sheet2" has to search it, which is what citations exist to avoid.

**One passage per row.** Chosen. A row is the record a skills workbook is
actually made of — one region, one occupation, one year — and its address
(``Sheet2!B4:F4``) is narrow enough that a reviewer lands on the exact cells.

That leaves the part that makes or breaks retrieval. A bare row serialised as
``North East; 12340; 11845`` embeds close to nothing: the numbers carry no
meaning without their columns, and a query about apprenticeship starts has no
lexical or semantic overlap with any of it. So **each value is written next to
its column header**::

    Apprenticeship starts by region. Region: North East; Starts 2023: 12,340

The header is repeated in every row of the sheet. That is the cost, and it is
worth paying: it is what makes a cell value mean something on its own, which is
the only way the row survives being pulled out of the workbook and dropped into
a retrieval index. Sheet name included, because it is part of the address the
locator already names, and it is often the only place the *subject* of the
table is written down.

What is deliberately **not** spliced in: the title and note rows above the
table. Those become their own passages, with their own anchors. Copying a title
from ``A1`` into a passage anchored at ``B47:F47`` would make the passage text
stop corresponding to the cells it cites, and that is the first step of exactly
the rot design rule 1 exists to prevent. A header is different in kind — it is
part of what the cell *means*, not context about it.
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

A row with one populated cell is far more often a footnote ("Source: DfE,
2025") than a data row with everything but its label blank, and pairing a
footnote against the first column's header produces the nonsense
``Region: Source: DfE, 2025``. Rows below this threshold are serialised as
their bare text instead. The cost is that a genuine one-value row loses its
column name; that is the milder of the two failures.
"""


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row, keyed by 0-indexed column, holding only populated cells.

    ``number`` is the row number the file itself shows — 1-indexed, and never
    renumbered around blank rows, for the same reason ``paragraph_index`` is
    not renumbered in the DOCX adapter: the locator has to address the real
    document so a reviewer opening it lands in the right place.
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

        Distinct from :attr:`columns`, and the distinction matters for headers:
        a header whose third label happens to be blank has three populated
        cells but spans four columns, the same four its data rows span. Judging
        it on populated count makes it look narrower than the table it heads,
        and the first data row wins the header search instead.
        """
        if not self.values:
            return []
        return list(range(min(self.values), max(self.values) + 1))


@dataclass(frozen=True, slots=True)
class Table:
    """One sheet's populated rows, in file order."""

    sheet: str
    rows: list[TableRow]


def column_label(index: int) -> str:
    """0-indexed column number to its spreadsheet letter (0 -> A, 26 -> AA)."""
    return str(get_column_letter(index + 1))


def format_value(value: object, *, number_format: str = "") -> str:
    """Render one cell as text, faithfully rather than as the sheet displays it.

    Excel's display format is a rendering, and applying it is a guess that can
    silently change the number — a cell holding 12.344 displayed to two places
    would be cited as 12.34. A reviewer checking a citation against a value that
    was quietly rounded is worse off than one reading the stored number, so the
    stored number is what goes in the passage.

    The single exception is a percentage format, which is not a guess: Excel
    *defines* it as the stored value times 100 with a per-cent sign, so a cell
    holding 0.04 and displaying "4%" is written ``4%``. Without this the draft
    would cite 0.04 where the workbook plainly shows 4%, and the citation would
    read as wrong to the only person who can sign it off.
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

    Sector skills workbooks rarely start at ``A1``. There is a title, a blank
    row, a note about the methodology, another blank, and only then the header.
    So the header cannot be assumed to be the first row, and it cannot be found
    by looking for text rather than numbers either — year columns ("2023",
    "2024") are numeric, and region names in the body are not.

    The signal that does hold is the *right edge* — the rightmost column a row
    reaches. Preamble and footnotes start at the left and stop early; every row
    of the table proper, header included, runs out to the table's last column.
    So: the header is the first row reaching the sheet's most common right
    edge, whose values are all distinct and not all numbers.

    Right edge rather than width, and the difference is not academic. It was
    width until a real ASIL workbook produced this, which is the ordinary shape
    of any comparison matrix::

        row 5:        | FYA New Basics (2016) | ASIL Research (2026)   <- header
        row 6: Data source | 4.2M job ads     | 950 job ads

    The header's top-left corner is empty, because the first column holds row
    labels and has nothing to be called. That makes the header two cells wide
    against the body's three, so a width test skips it and crowns row 6 — and
    every row below is then captioned with a data value, citing
    ``Data source: Reference period``. Both rows end at the same column, so the
    right edge picks the header out and row order does the rest.

    Distinctness is what stops a merged title from winning. A title merged
    across ``A1:F1`` propagates to six cells (see the merge note in the XLSX
    adapter) and would otherwise look exactly as wide as the table; six copies
    of one string give it away, whereas real column headers are never
    duplicates.

    One more condition, because width alone will happily crown the first row of
    a sheet that has no header at all: a header must contain **at least one
    cell that is not a number**. A table of bare figures then finds no header
    and falls back to column letters, instead of pairing row 2 against row 1
    and citing ``12340: 31200`` — a caption invented out of data.

    And a last one: **a header does not repeat itself down its own column.** A
    column called "Risk Group" is named once; a data row's "Higher risk" appears
    again in every row beneath it. This is what separates a genuine header from
    the first data row of a block that has been cut off from its header, which
    happens in ASIL's DOCX tables — a banner row splits the data from the header
    above it, and the first data row would otherwise be crowned, captioning the
    rest as ``Manufacturing: Agriculture, Forestry & Fishing``. Blocks with no
    header of their own inherit the last one seen; see :func:`table_to_passages`.

    Returning ``None`` is a real answer, not a failure. A sheet with no header
    gets column letters, which is honest — the alternative is inventing names
    for columns, and rule 2 says the null path stays open.
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

    Counter breaks ties by first appearance, which is row order, so a sheet
    with one row of each shape prefers the earlier — the header, since the data
    it heads comes after it.
    """
    edges = [max(row.values) for row in rows if row.populated >= 2]
    if not edges:
        return None
    return Counter(edges).most_common(1)[0][0]


def table_columns(rows: Sequence[TableRow], header: TableRow | None) -> set[int]:
    """The contiguous column footprint of the table body.

    Needed separately from the header's own columns because of the empty-corner
    matrix above: the header covers B to C while its rows cover A to C, and pairing
    against the header's columns alone would leave every row unpaired. The
    footprint runs from the leftmost column any full-width row uses out to the
    table's right edge, so column A gets a label — its letter, since the sheet
    never gave it a name.

    Computed over every row rather than the first :data:`HEADER_SEARCH_ROWS`,
    because a table can be longer than the header search needs to look.
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

    Deliberately generous about decoration, because real reports decorate:
    ``54.4%``, ``$109,860``, ``~74%`` and ``+31.0pp`` are all one number wearing
    something. What matters is the distinction from a label like
    ``Higher Risk Industries (avg %)``, which contains a per-cent sign and is
    still prose.
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

    The test is a difference in kind: in at least one column the candidate is a
    label while most of the rows below it are measurements. ``Avg Salary Low
    (AUD)`` over a column of ``$109,860`` passes; ``Leadership | 37.1% | 40.4%``
    over more rows of percentages does not, because it is simply the first row
    of a table whose header is somewhere above.

    Used only to decide whether a block that *could* inherit a header should
    instead declare its own — see :func:`table_to_passages`. A block with no
    header to inherit takes whatever :func:`find_header_row` offers, because
    something is better than column letters and there is nothing to compare
    against.
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

    An unnamed column is called "Column D" rather than being given a plausible
    name. Naming it would be a small confabulation in exactly the place the
    system is meant to abstain.
    """
    labels: dict[int, str] = {}
    for index in columns:
        label = header.values.get(index, "").strip() if header else ""
        labels[index] = label or f"Column {column_label(index)}"
    return labels


def cell_range(row_number: int, columns: Sequence[int]) -> str:
    """A1 notation for the populated span of one row: ``B4:F4``, or ``B4``.

    The span runs from the first populated column to the last, so it is what a
    reviewer can paste into the name box and land on. A single cell collapses to
    the short form rather than the redundant ``B4:B4``.
    """
    if not columns:
        raise ValueError(f"row {row_number} has no populated columns to address")
    first = f"{column_label(columns[0])}{row_number}"
    if len(columns) == 1:
        return first
    return f"{first}:{column_label(columns[-1])}{row_number}"


def collapse_whitespace(text: str) -> str:
    """Runs of whitespace become one space.

        A workbook header is laid out to look right in a cell, so it contains hard
        line breaks: ``Enterprise Skill
    (ASIL framework)`` is one label wearing two
        lines. Carried into a passage those newlines make a row read as several
        lines and make the label read as two, and a citation quoting the row shows
        the reviewer something that does not look like what the passage says.

        Applied here, in serialisation, rather than in either adapter: XLSX and CSV
        must produce identical text for the same table, and one shared place is what
        guarantees that. (``adapters/docx.py`` also collapses at parse time, where it
        is joining several paragraphs inside one cell; applying it twice is
        harmless.)
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
    — titles, notes, anything too narrow or too far outside the table's columns
    to trust a header against — are written as their bare values, because a
    label guessed onto a footnote is worse than no label.

    Empty cells contribute nothing at all. There is no ``Header: —`` and no
    "N/A": a column with no value for this row is a fact about the workbook, and
    inventing a placeholder for it is the confabulation rule 2 forbids.
    """
    parts: list[str] = []
    for index in row.columns:
        value = collapse_whitespace(row.values[index])
        if not value:
            continue
        # ``labels`` is empty for an unpaired row, so it is only consulted when
        # there is a header to consult.
        parts.append(
            f"{collapse_whitespace(labels[index])}: {value}" if paired else value
        )
    body = "; ".join(parts)
    return f"{sheet}. {body}" if sheet else body


def split_tables(rows: Sequence[TableRow]) -> list[list[TableRow]]:
    """One sheet's rows cut into blocks, one per table on the sheet.

    A sheet is not a table. A real ASIL workbook stacks two of them under one
    tab — "FYA (2016): Pay Premium…" over rows 4 to 9, a banner on row 11, then
    "ASIL (2026): Average Advertised Salary…" over rows 12 to 31 — each with its
    own header. Treating the sheet as one table applies the first header to the
    second table's rows, so ``$109,860`` in the *Avg Salary Low* column gets
    cited as ``Pay Premium (AUD/year): $109,860``: a plausible sentence, a sound
    anchor, and the wrong column name. That is the exact failure this project
    cannot survive, and it does not announce itself.

    The cut is made at rows too narrow to belong to a table — a banner, title or
    note, one cell wide. That is also how a person reads the sheet: the heading
    over a block of rows is what says a new block has started. Each separator
    stays in the output as a block of its own, so nothing is dropped and every
    row keeps its own anchor.
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
    """Every populated row of one sheet, as anchored passages.

    Header detection runs per block, not per sheet — see :func:`split_tables`.

    A block with no header of its own **inherits the last one seen**, provided
    the two line up on the right. Both behaviours are needed, and real files
    demand them within one document:

    - ``Salary Premiums`` stacks two tables under one tab with a banner between,
      and the second has its own header. Inheriting there would caption an
      average salary as a pay premium.
    - A DOCX table in ``ASIL_Jobs_of_the_Future.docx`` has one header at the top
      and banner rows subdividing the data below it ("Higher automation risk
      industries", "Lower automation risk industries"). Not inheriting there
      leaves eight rows captioned by the first data row.

    What separates them is whether the block contains a row that looks like a
    header at all, which is the last condition in :func:`find_header_row`.
    """
    passages: list[Passage] = []
    carried_columns: set[int] = set()
    carried_labels: Mapping[int, str] = {}

    for block in split_tables(table.rows):
        candidate = find_header_row(block)
        inheritable = _fits(block, carried_columns)
        # A block that could inherit only declares its own header when that
        # header reads as one for the rows beneath it. Otherwise the first row
        # of an orphaned block gets crowned, and captions the rest of its own
        # table: "Leadership: Presentation; 37.1%: 2.0%".
        header = (
            candidate
            if candidate is not None
            and (not inheritable or introduces_new_header(candidate, block))
            else None
        )
        if header is not None:
            # The table's footprint, not the header's own cells. A blank label
            # mid-header still describes the column under it, and an empty
            # top-left corner still has a column of row labels beneath it; both
            # get their letter rather than leaving the whole row unpaired.
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

    Requiring the same right edge, not merely that the columns overlap: a
    narrower table further down the sheet is a different table, and borrowing
    labels across the two would put the wrong column name on every row of it.
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

        # Pair against headers only where the header actually covers the row.
        # A footnote sitting in a column the table never used is not data, and
        # pairing it would attach a column name to something that has none.
        # ``labels`` may come from this block's own header or from an
        # inherited one, so the test is whether labels exist and cover the row,
        # not whether this block found a header itself.
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
