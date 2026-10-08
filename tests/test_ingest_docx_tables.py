"""DOCX table extraction (BUILD_SPEC.md section 3).

A DOCX table cell is tabular data, so it goes through the same serialisation the
XLSX adapter uses and carries a :class:`~updater.models.CellLocator`. The tests
that matter here are about the *label*, not the range.

Both header bugs found on real ASIL files produced correct cell ranges with
wrong column names — ``Data source: Reference period``, ``Pay Premium
(AUD/year): $109,860`` — and a range assertion cannot see either of them. So the
central test resolves a known cell to its table, row, column *and header label*,
and a mutant that shifts labels by one column proves the label half has teeth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    BANNERED_TABLE,
    NUMERIC_BANNERED_TABLE,
    cell_locators,
    make_source,
    write_docx_with_table,
)
from updater.ingest import extract
from updater.ingest.adapters.docx import table_name
from updater.models import BlockLocator, CellLocator, MediaType, Passage

REAL_DOCX = Path("data/sources/ASIL_Jobs_of_the_Future.docx")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def cells(passages: list[Passage]) -> dict[tuple[str, str], str]:
    """``{(table identity, cell range): text}`` for the cell-anchored passages."""
    found = {}
    for passage in passages:
        locator = passage.anchor.locator
        if isinstance(locator, CellLocator):
            found[(locator.sheet, locator.cell_range)] = passage.text
    return found


def extract_fixture(tmp_path: Path) -> list[Passage]:
    path = write_docx_with_table(tmp_path / "report.docx")
    return extract(path, make_source(media_type=MediaType.DOCX))


# --------------------------------------------------------------------------- #
# The locator
# --------------------------------------------------------------------------- #


def test_a_table_cell_resolves_to_table_row_column_and_header(tmp_path: Path) -> None:
    """The whole contract in one assertion.

    "Mining" is row 3 of the table's data, column A, and its automation-risk
    figure sits in column C under a header reading "Est. Automation Risk". A
    citation has to carry all four facts or a reviewer cannot check it.
    """
    passages = extract_fixture(tmp_path)
    table = table_name(1, 1)

    row = cells(passages)[(table, "A5:D5")]

    assert "Industry: Mining" in row
    assert "Est. Automation Risk: ~55%" in row
    assert "Risk Group: Higher risk" in row
    assert "Role Type: Resource extraction" in row


def test_table_identity_names_the_table_and_where_it_sits(tmp_path: Path) -> None:
    """``Table 1 (para 1)``: the first table, sitting above paragraph 1."""
    passages = extract_fixture(tmp_path)

    sheets = {locator.sheet for locator in cell_locators_of(passages)}

    assert sheets == {"Table 1 (para 1)"}


def cell_locators_of(passages: list[Passage]) -> list[CellLocator]:
    tabular = [
        passage
        for passage in passages
        if isinstance(passage.anchor.locator, CellLocator)
    ]
    return cell_locators(tabular)


def test_table_row_numbers_are_within_the_table_not_the_document(
    tmp_path: Path,
) -> None:
    """``B4`` means four rows down *that table*, which is what a reviewer counts."""
    passages = extract_fixture(tmp_path)

    ranges = [locator.cell_range for locator in cell_locators_of(passages)]

    assert ranges == ["A1:D1", "A2", "A3:D3", "A4:D4", "A5:D5", "A6", "A7:D7", "A8:D8"]


# --------------------------------------------------------------------------- #
# Document order
# --------------------------------------------------------------------------- #


def test_tables_interleave_with_paragraphs_in_document_order(tmp_path: Path) -> None:
    """A table's position is part of its meaning.

    The paragraph above a table is what says what the table shows. Appending
    tables at the end would put every one of them next to the wrong text, and
    the chunking stage would then build neighbourhoods that do not exist.
    """
    passages = extract_fixture(tmp_path)

    kinds = [type(passage.anchor.locator).__name__ for passage in passages]

    assert kinds[0] == "BlockLocator"
    assert kinds[-1] == "BlockLocator"
    assert set(kinds[1:-1]) == {"CellLocator"}


def test_paragraph_indices_ignore_tables(tmp_path: Path) -> None:
    """A table between two paragraphs does not consume a paragraph index.

    ``paragraph_index`` addresses ``document.paragraphs``, and a reviewer
    counting blocks in Word counts the same way.
    """
    passages = extract_fixture(tmp_path)

    blocks = [
        passage.anchor.locator
        for passage in passages
        if isinstance(passage.anchor.locator, BlockLocator)
    ]

    assert [locator.paragraph_index for locator in blocks] == [0, 1]


# --------------------------------------------------------------------------- #
# The label, and the mutant that proves the assertion
# --------------------------------------------------------------------------- #


def test_a_banner_row_inside_a_table_does_not_steal_the_header(
    tmp_path: Path,
) -> None:
    """Regression, from ASIL_Jobs_of_the_Future.docx.

    The banner splits the header away from the rows below it. Without carrying
    the header across, the first data row is crowned and every row after it
    reads ``Industry: Agriculture, Forestry & Fishing``.
    """
    passages = extract_fixture(tmp_path)
    table = table_name(1, 1)
    found = cells(passages)

    # The banner survives as its own passage rather than being consumed.
    assert found[(table, "A6")].endswith(
        "Lower automation risk industries (10 industries)"
    )
    # And the rows below it still know their columns.
    assert "Industry: Health Care & Social Assistance" in found[(table, "A7:D7")]
    assert "Industry: Manufacturing" not in found[(table, "A7:D7")]


def test_a_horizontally_merged_banner_is_one_cell_not_four(tmp_path: Path) -> None:
    """python-docx repeats one <w:tc> across every column a merge covers."""
    passages = extract_fixture(tmp_path)
    table = table_name(1, 1)

    banner = cells(passages)[(table, "A2")]

    assert banner.endswith("Higher automation risk industries (9 industries)")
    assert "Risk Group" not in banner


def test_the_header_label_assertion_catches_a_shifted_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation test for the label half, which no cell-range test can reach.

    Both header bugs found on real ASIL files produced perfectly correct ranges
    and wrong column names. This mutant reproduces that class exactly: labels
    slide one column left, so every value keeps its anchor and loses its
    meaning. If the assertions above ever stop checking labels, this fails.
    """
    import updater.ingest.tabular as tabular

    original = tabular.header_labels

    def shifted(header: object, columns: object) -> dict[int, str]:
        labels = original(header, columns)  # type: ignore[arg-type]
        keys = sorted(labels)
        return {
            key: labels[keys[(index + 1) % len(keys)]] for index, key in enumerate(keys)
        }

    monkeypatch.setattr(tabular, "header_labels", shifted)

    passages = extract_fixture(tmp_path)
    row = cells(passages)[(table_name(1, 1), "A5:D5")]

    assert "Industry: Mining" not in row
    assert "Risk Group: Mining" in row


# --------------------------------------------------------------------------- #
# Against the real document, when it is present
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not REAL_DOCX.exists(),
    reason="client material is not in the repo; run with data/sources populated",
)
def test_a_known_cell_of_a_real_asil_table(tmp_path: Path) -> None:
    """The same contract, against the actual client document.

    The fixture above reproduces this table's shape; this asserts the shape was
    reproduced faithfully. Skipped in any checkout without the client files,
    which is every checkout but this one — the documents are not in the repo.
    """
    passages = extract(REAL_DOCX, make_source(media_type=MediaType.DOCX))
    found = cells(passages)
    table = table_name(1, 5)

    assert (table, "A1:D1") in found
    mining = found[(table, "A5:D5")]

    assert "Industry: Mining" in mining
    assert "Risk Group: Higher risk" in mining
    assert "Est. Automation Risk: ~55%" in mining


def test_the_fixture_matches_the_real_table_it_stands_for() -> None:
    """Guards the fixture: the tests above encode these labels and rows."""
    assert BANNERED_TABLE[0] == (
        "Industry",
        "Risk Group",
        "Est. Automation Risk",
        "Role Type",
    )
    assert BANNERED_TABLE[4][0] == "Mining"


def test_an_orphaned_numeric_block_does_not_crown_its_first_row(
    tmp_path: Path,
) -> None:
    """Regression, from table 2 of ASIL_Jobs_of_the_Future.docx.

    Every data column here is a measurement, so nothing repeats down a column
    and the first row of a banner-orphaned block satisfies every structural test
    for a header. It got crowned, and the rows below it read
    ``Leadership: Presentation; 37.1%: 2.0%`` — anchors correct, labels drawn
    from a data row.

    What tells the two apart is kind: the real header is a label sitting over a
    column of numbers; the impostor is a number sitting over more numbers.
    """
    path = write_docx_with_table(
        tmp_path / "numeric.docx",
        table=NUMERIC_BANNERED_TABLE,
        banner_rows=(2, 5),
    )

    passages = extract(path, make_source(media_type=MediaType.DOCX))
    found = cells(passages)
    table = table_name(1, 1)

    for cell_range in ("A3:C3", "A6:C6", "A7:C7"):
        assert "Enterprise Skill:" in found[(table, cell_range)]
        assert "Higher Risk Industries (avg %):" in found[(table, cell_range)]

    assert "Enterprise Skill: Presentation" in found[(table, "A7:C7")]
    # The old failure, in full.
    assert "Leadership: Presentation" not in found[(table, "A7:C7")]


def test_a_genuinely_new_header_after_a_banner_is_still_honoured(
    tmp_path: Path,
) -> None:
    """The other half: inheritance must not swallow a real second header.

    Two stacked tables, the second with its own header, is the ASIL
    "Salary Premiums" shape. Inheriting there captions an average salary as a
    pay premium.
    """
    table = (
        ("Enterprise Skill", "Pay Premium", "Context"),
        ("Presentation", "$8,853", "Highest premium"),
        ("ASIL 2026: Average Advertised Salary by Industry",),
        ("Industry Division", "Avg Salary Low", "Avg Salary High"),
        ("Professional, Scientific & Technical", "$109,860", "$160,000"),
    )
    path = write_docx_with_table(
        tmp_path / "stacked.docx", table=table, banner_rows=(3,)
    )

    passages = extract(path, make_source(media_type=MediaType.DOCX))
    found = cells(passages)[(table_name(1, 1), "A5:C5")]

    assert "Industry Division: Professional, Scientific & Technical" in found
    assert "Avg Salary Low: $109,860" in found
    assert "Pay Premium" not in found
