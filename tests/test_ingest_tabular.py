"""XLSX and CSV adapter tests (BUILD_SPEC.md section 3).

The assertion that matters here is the same one the PDF tests make about page
numbers, and it matters for the same reason: a wrong ``cell_range`` produces a
draft full of citations that look perfectly plausible and send a reviewer to the
wrong cells. Text extraction failing is loud; ``B4:F4`` when the figure is
really on row 5 is not.

So the central test does not check that a passage *contains* the right values —
that would still pass if the adapter had smeared three rows together. It reopens
the workbook, reads the cells the locator names, and checks both directions:
every value in those cells is in the passage, and no value from the rows either
side of them is. ``test_the_cell_range_assertion_catches_an_off_by_one`` then
mutates the adapter to prove that check has teeth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from conftest import (
    GAPS_SHEET,
    MATRIX_SHEET,
    MERGED_SHEET,
    STARTS_SHEET,
    TWO_TABLE_SHEET,
    cell_locators,
    make_source,
    write_csv,
    write_skills_workbook,
    write_xlsx,
)
from updater.ingest import ExtractionFailed, extract, sniff_media_type
from updater.ingest.tabular import (
    Table,
    TableRow,
    cell_range,
    find_header_row,
    split_tables,
)
from updater.models import MediaType, Passage

# --------------------------------------------------------------------------- #
# The cell_range guarantee
# --------------------------------------------------------------------------- #


def read_range(path: Path, sheet: str, a1: str) -> list[str]:
    """The non-empty values openpyxl finds at ``a1``, as text.

    Deliberately a second, dumber reader than the adapter's. If both shared a
    code path, a bug in the shared part would be invisible to every assertion
    built on this.

    Per-cent cells are the one thing it skips. The adapter renders 0.04 as "4%"
    on purpose, so comparing raw against rendered would fail on a difference
    that is correct; ``test_a_percentage_is_rendered_as_the_workbook_shows_it``
    covers those cells directly instead.
    """
    workbook = load_workbook(str(path), data_only=True)
    try:
        worksheet = workbook[sheet]
        cells = worksheet[a1]
        rows = cells if isinstance(cells, tuple) else ((cells,),)
        values = []
        for row in rows:
            row_cells = row if isinstance(row, tuple) else (row,)
            for cell in row_cells:
                if "%" in str(cell.number_format or ""):
                    continue
                if cell.value is not None and str(cell.value).strip():
                    values.append(str(cell.value).strip())
        return values
    finally:
        workbook.close()


def assert_range_matches_workbook(passages: list[Passage], path: Path) -> None:
    """Every passage says what its cells say, and nothing its neighbours say.

    The negative half is the half that catches real bugs. A reader that ran two
    rows together would satisfy "contains the right values" perfectly.
    """
    for passage in passages:
        locator = cell_locators([passage])[0]
        a1 = locator.cell_range
        # From the first coordinate only. Scanning the whole of "A6:D6" for
        # digits yields "66", which addresses a row that does not exist and
        # quietly turns the neighbour check below into a no-op — which is what
        # test_the_cell_range_assertion_catches_a_smeared_row caught.
        start = a1.split(":")[0]
        row_number = int(
            "".join(character for character in start if character.isdigit())
        )

        for value in read_range(path, locator.sheet, a1):
            assert value in passage.text, (
                f"{passage.id} claims {locator.sheet}!{a1}, which holds {value!r}, "
                f"but its text does not contain it: {passage.text!r}"
            )

        columns = a1.split(":")[0].rstrip("0123456789")
        for neighbour in (row_number - 1, row_number + 1):
            if neighbour < 1:
                continue
            neighbour_range = f"{columns}{neighbour}:Z{neighbour}"
            for value in read_range(path, locator.sheet, neighbour_range):
                # A label legitimately repeats down a merged column, and short
                # values coincide across rows constantly; neither is diagnostic.
                if len(value) < 4 or value in read_range(path, locator.sheet, a1):
                    continue
                # A neighbour value in *label* position is the header doing its
                # job — "Region: North East" on row 6 quotes row 5 on purpose.
                # The same string anywhere else is a row boundary that leaked.
                if f"{value}: " in passage.text:
                    continue
                assert value not in passage.text, (
                    f"{passage.id} claims {locator.sheet}!{a1} but contains "
                    f"{value!r}, which is on row {neighbour}"
                )


def test_xlsx_passages_are_cell_anchored(tmp_path: Path) -> None:
    """The locator variant and every cell_range value, pinned exactly."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    locators = cell_locators(passages)

    assert [(locator.sheet, locator.cell_range) for locator in locators] == [
        # Row 1 is a title merged across A1:D1. Its address is A1: the merge is
        # one cell, and pretending otherwise invents three more.
        ("Apprenticeship starts", "A1"),
        ("Apprenticeship starts", "A3"),
        ("Apprenticeship starts", "A5:D5"),
        ("Apprenticeship starts", "A6:D6"),
        ("Apprenticeship starts", "A7:D7"),
        # Row 8 is blank and is not renumbered away.
        ("Apprenticeship starts", "A9:D9"),
        ("Apprenticeship starts", "A10:D10"),
        ("Apprenticeship starts", "A12"),
        ("Skills gaps", "A1:D1"),
        ("Skills gaps", "A2:D2"),
        ("Skills gaps", "A3:D3"),
    ]


def test_a_passage_says_what_its_cells_say_and_nothing_else(tmp_path: Path) -> None:
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert_range_matches_workbook(passages, path)


def test_the_cell_range_assertion_catches_an_off_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation test: shift every row number by one, and the check must fail.

    A test that passes is only worth having if it would also fail. This mutant
    is the likeliest real bug in the whole adapter — a row number out by one,
    which produces citations wrong in the least visible possible way, every one
    of them landing on a row that exists and looks plausible.

    Without this, ``test_a_passage_says_what_its_cells_say_and_nothing_else``
    could be quietly reduced to nothing — by a bug in ``read_range``, say — and
    would go on reporting success forever.
    """
    import updater.ingest.tabular as tabular

    def shifted(row_number: int, columns: list[int]) -> str:
        return cell_range(row_number + 1, columns)

    monkeypatch.setattr(tabular, "cell_range", shifted)

    path = write_skills_workbook(tmp_path / "skills.xlsx")
    passages = extract(path, make_source(media_type=MediaType.XLSX))

    with pytest.raises(AssertionError, match=r"which is on row|does not contain it"):
        assert_range_matches_workbook(passages, path)


def test_the_cell_range_assertion_catches_a_smeared_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation test for the *negative* half, which the off-by-one may not reach.

    This mutant bleeds each row's neighbour into it — the tabular equivalent of
    the PDF reader that concatenated adjacent pages. Every anchor still points
    at a row holding every value the passage names, so a check that only asked
    "does the passage contain what these cells hold" would pass it happily. Only
    asserting the *absence* of the neighbour's values catches it.
    """
    from updater.ingest.adapters import xlsx as xlsx_adapter

    original = xlsx_adapter.XlsxExtractor._read_sheet

    def smeared(worksheet: object) -> Table:
        table = original(worksheet)  # type: ignore[arg-type]
        rows = table.rows
        bled = []
        for index, row in enumerate(rows):
            following = rows[index + 1].values if index + 1 < len(rows) else {}
            # Same columns, same anchor — only the text runs the two together,
            # which is precisely what a reader that lost its row boundary does.
            values = {
                column: f"{value} {following[column]}".strip()
                if column in following
                else value
                for column, value in row.values.items()
            }
            bled.append(TableRow(number=row.number, values=values))
        return Table(sheet=table.sheet, rows=bled)

    monkeypatch.setattr(
        xlsx_adapter.XlsxExtractor, "_read_sheet", staticmethod(smeared)
    )

    path = write_skills_workbook(tmp_path / "skills.xlsx")
    passages = extract(path, make_source(media_type=MediaType.XLSX))

    with pytest.raises(AssertionError, match="which is on row"):
        assert_range_matches_workbook(passages, path)


def test_single_cell_rows_use_the_short_a1_form(tmp_path: Path) -> None:
    """``A12``, not ``A12:A12`` — the form a reviewer can paste into Excel."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    ranges = [locator.cell_range for locator in cell_locators(passages)]

    assert "A12" in ranges
    assert "A12:A12" not in ranges


# --------------------------------------------------------------------------- #
# Header handling
# --------------------------------------------------------------------------- #


def test_header_is_found_below_a_preamble(tmp_path: Path) -> None:
    """Row 5, under a title, a note and two blanks."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    row_six = next(
        passage
        for passage in passages
        if cell_locators([passage])[0].cell_range == "A6:D6"
    )

    assert "Region: North East" in row_six.text
    assert "Starts 2024: 11845" in row_six.text


def test_a_merged_title_does_not_win_the_header_search(tmp_path: Path) -> None:
    """The title spans the table's full width; distinctness is what rejects it."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    title = next(
        passage
        for passage in passages
        if cell_locators([passage])[0].cell_range == "A1"
    )

    assert title.text.endswith("Table 3: Apprenticeship starts by region")
    # Not "Region: Table 3: ...; Starts 2023: Table 3: ..." — pairing a banner
    # against the headers produces four citations to one cell that says nothing.
    assert "Region:" not in title.text


def test_a_sheet_of_bare_numbers_finds_no_header_and_invents_none(
    tmp_path: Path,
) -> None:
    """Rule 2. Row 1 is not a header just because it happens to be first.

    Pairing row 2 against row 1 here would produce ``12340: 31200`` — a column
    name manufactured out of data, attached to a citation that looks sound. No
    header is the honest answer, and the values go in unlabelled.
    """
    rows = ((None, 12340, 11845), (None, 31200, 32015))
    path = write_xlsx(tmp_path / "bare.xlsx", {"Data": rows})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert passages[0].text == "Data. 12340; 11845"
    assert passages[1].text == "Data. 31200; 32015"
    assert [locator.cell_range for locator in cell_locators(passages)] == [
        "B1:C1",
        "B2:C2",
    ]


def test_an_unnamed_column_inside_a_header_falls_back_to_its_letter(
    tmp_path: Path,
) -> None:
    """The other half of rule 2: name what you can, letter what you cannot.

    Column C has a header cell but nothing in it. "Column C" says exactly what
    is known about that column, which is where it is.
    """
    rows = (("Region", "Starts", None, "Density"), ("North East", 12340, 99, 0.34))
    path = write_xlsx(tmp_path / "gap.xlsx", {"Data": rows})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert "Region: North East" in passages[1].text
    assert "Column C: 99" in passages[1].text


def test_find_header_row_returns_none_when_there_is_nothing_to_find() -> None:
    assert find_header_row([]) is None


def test_a_trailing_note_is_not_paired_against_the_headers(tmp_path: Path) -> None:
    """``Region: Figures are provisional`` would be a citation to nonsense."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    note = next(
        passage
        for passage in passages
        if cell_locators([passage])[0].cell_range == "A12"
    )

    assert note.text.endswith("Figures for 2024 are provisional.")
    assert "Region:" not in note.text


# --------------------------------------------------------------------------- #
# Workbook shapes
# --------------------------------------------------------------------------- #


def test_blank_rows_are_skipped_without_renumbering(tmp_path: Path) -> None:
    """Row 8 is blank; row 9 must still be cited as row 9."""
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    ranges = [locator.cell_range for locator in cell_locators(passages)]

    assert "A8:D8" not in ranges
    assert "A9:D9" in ranges
    yorkshire = next(passage for passage in passages if "Yorkshire" in passage.text)
    assert cell_locators([yorkshire])[0].cell_range == "A9:D9"


def test_each_sheet_keeps_its_own_name(tmp_path: Path) -> None:
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    sheets = {locator.sheet for locator in cell_locators(passages)}

    assert sheets == {"Apprenticeship starts", "Skills gaps"}
    for passage in passages:
        locator = cell_locators([passage])[0]
        if locator.sheet == "Skills gaps":
            assert "Region" not in passage.text


def test_a_vertical_merge_labels_every_row_it_spans(tmp_path: Path) -> None:
    """A2:A4 holds one label; rows 3 and 4 would otherwise be bare numbers."""
    path = write_xlsx(
        tmp_path / "merged.xlsx",
        {"Providers": MERGED_SHEET},
        merges={"Providers": ("A2:A4",)},
    )

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    for passage in passages[1:]:
        assert "Provider type: Further education college" in passage.text
    assert [locator.cell_range for locator in cell_locators(passages)] == [
        "A1:C1",
        "A2:C2",
        "A3:C3",
        "A4:C4",
    ]


def test_empty_cells_get_no_placeholder(tmp_path: Path) -> None:
    """Rule 2: a column with no value for this row contributes nothing."""
    rows = (("Region", "Starts", "Notes"), ("North East", 12340, None))
    path = write_xlsx(tmp_path / "gappy.xlsx", {"Data": rows})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert "Notes" not in passages[1].text
    assert "N/A" not in passages[1].text
    assert cell_locators(passages)[1].cell_range == "A2:B2"


def test_a_percentage_is_rendered_as_the_workbook_shows_it(tmp_path: Path) -> None:
    """0.04 under a per-cent format is cited as 4%, not as 0.04.

    A reviewer opening D6 sees "-4.0%". A draft citing "-0.04" against that cell
    reads as a wrong citation, which is the one thing this project cannot have.
    """
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    row_six = next(
        passage
        for passage in passages
        if cell_locators([passage])[0].cell_range == "A6:D6"
    )

    assert "Change on year: -4%" in row_six.text
    assert "0.04" not in row_six.text


def test_an_entirely_empty_workbook_fails_loudly(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "empty.xlsx", {"Sheet1": ((), ())})

    with pytest.raises(ExtractionFailed, match="no sheet with any populated rows"):
        extract(path, make_source(media_type=MediaType.XLSX))


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #


def test_a_csv_no_longer_sniffs_as_plain_text(tmp_path: Path) -> None:
    """The dispatch bug: text/plain would send a table to whatever reads prose."""
    path = write_csv(tmp_path / "starts.csv", GAPS_SHEET)

    assert sniff_media_type(path) == MediaType.CSV


def test_prose_still_sniffs_as_plain_text(tmp_path: Path) -> None:
    """Consistency of field count is the test; English commas are not consistent."""
    path = tmp_path / "note.txt"
    path.write_text(
        "Sustained destinations rose to 68% of the cohort, measured six months\n"
        "after key stage 4. Coverage of independent providers is incomplete.\n"
        "Comparisons with years before 2019, in particular, are not advised.\n",
        encoding="utf-8",
    )

    assert sniff_media_type(path) == MediaType.TXT


def test_a_semicolon_export_is_detected_and_read(tmp_path: Path) -> None:
    """What Excel writes on a European locale, where comma is the decimal mark."""
    path = write_csv(tmp_path / "euro.csv", GAPS_SHEET, delimiter=";")

    assert sniff_media_type(path) == MediaType.CSV
    passages = extract(path, make_source(media_type=MediaType.CSV))

    assert "Occupation: Care worker" in passages[1].text
    assert cell_locators(passages)[1].cell_range == "A2:D2"


def test_csv_sheet_is_the_file_stem(tmp_path: Path) -> None:
    """A CSV has no sheet, so the citation names the file instead."""
    path = write_csv(tmp_path / "sector-skills-2025.csv", GAPS_SHEET)

    passages = extract(path, make_source(media_type=MediaType.CSV))

    assert {locator.sheet for locator in cell_locators(passages)} == {
        "sector-skills-2025"
    }


def test_csv_and_xlsx_serialise_the_same_table_identically(tmp_path: Path) -> None:
    """The reason the two adapters share a module.

    If these drift, the retrieval index depends on which format somebody
    happened to save, and two runs of the same source stop being comparable.
    """
    csv_path = write_csv(tmp_path / "same.csv", GAPS_SHEET)
    xlsx_path = write_xlsx(tmp_path / "same.xlsx", {"same": GAPS_SHEET})

    csv_passages = extract(csv_path, make_source(media_type=MediaType.CSV))
    xlsx_passages = extract(xlsx_path, make_source(media_type=MediaType.XLSX))

    assert [passage.text for passage in csv_passages] == [
        passage.text for passage in xlsx_passages
    ]
    assert [locator.cell_range for locator in cell_locators(csv_passages)] == [
        locator.cell_range for locator in cell_locators(xlsx_passages)
    ]


def test_csv_blank_lines_are_skipped_without_renumbering(tmp_path: Path) -> None:
    rows = (("Region", "Starts"), (), ("North East", 12340))
    path = write_csv(tmp_path / "gappy.csv", rows)

    passages = extract(path, make_source(media_type=MediaType.CSV))

    assert [locator.cell_range for locator in cell_locators(passages)] == [
        "A1:B1",
        "A3:B3",
    ]


def test_a_quoted_delimiter_does_not_split_a_field(tmp_path: Path) -> None:
    """``"North East, England"`` is one cell, and the range must say so."""
    path = tmp_path / "quoted.csv"
    path.write_text(
        'Region,Starts\n"North East, England",12340\n"North West, England",31200\n',
        encoding="utf-8",
    )

    passages = extract(path, make_source(media_type=MediaType.CSV))

    assert "Region: North East, England" in passages[1].text
    assert cell_locators(passages)[1].cell_range == "A2:B2"


# --------------------------------------------------------------------------- #
# The serialisation itself
# --------------------------------------------------------------------------- #


def test_a_value_is_written_next_to_its_column_header(tmp_path: Path) -> None:
    """The decision this whole module turns on.

    ``11845`` on its own is unretrievable and unreadable. The header is what
    makes it mean something once the row has been pulled out of the workbook.
    """
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    row_six = next(passage for passage in passages if "North East" in passage.text)

    assert "Starts 2024: 11845" in row_six.text
    assert "; 11845;" not in row_six.text


def test_the_sheet_name_is_carried_but_the_title_row_is_not(tmp_path: Path) -> None:
    """Address, yes; content from other cells, no.

    The sheet name is part of what the locator already names. The title at A1 is
    content, and splicing it into a passage anchored at A6:D6 would make the
    text stop corresponding to the cells it cites.
    """
    path = write_skills_workbook(tmp_path / "skills.xlsx")

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    row_six = next(passage for passage in passages if "North East" in passage.text)

    assert row_six.text.startswith("Apprenticeship starts.")
    assert "Table 3" not in row_six.text


def test_cell_range_rejects_a_row_with_no_columns() -> None:
    with pytest.raises(ValueError, match="no populated columns"):
        cell_range(4, [])


def test_column_letters_survive_past_z(tmp_path: Path) -> None:
    """Column 27 is AA. An off-by-one here is invisible until a wide workbook."""
    header = tuple(f"Col{index}" for index in range(28))
    data = tuple(range(28))
    path = write_xlsx(tmp_path / "wide.xlsx", {"Wide": (header, data)})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert cell_locators(passages)[0].cell_range == "A1:AB1"


def test_dates_are_written_as_iso_not_as_a_timestamp(tmp_path: Path) -> None:
    from datetime import datetime

    rows = (("Measure", "As at"), ("Sustained rate", datetime(2025, 3, 31)))
    path = write_xlsx(tmp_path / "dated.xlsx", {"Data": rows})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert "As at: 2025-03-31" in passages[1].text
    assert "00:00:00" not in passages[1].text


def test_the_quality_gate_accepts_a_numeric_row(tmp_path: Path) -> None:
    """Carrying headers is also what keeps a numeric table above the gate's floor.

    A bare ``5223; 1,204; 0.34; -2.1%`` scores 0.54 alphanumeric and would fail
    the whole workbook. Paired with its headers the same row scores about 0.69.
    """
    rows = (("SOC code", "Vacancies", "Density"), (5223, 1204, 0.34))
    path = write_xlsx(tmp_path / "numeric.xlsx", {"Gaps": rows})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert len(passages) == 2


def test_the_header_row_is_kept_as_a_passage(tmp_path: Path) -> None:
    """Not evidence, but not droppable either.

    Silently discarding a row is the behaviour BUILD_SPEC.md section 3 rules
    out, and what a table's columns are called is a legitimate citable fact.
    """
    path = write_xlsx(tmp_path / "gaps.xlsx", {"Gaps": GAPS_SHEET})

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    assert cell_locators(passages)[0].cell_range == "A1:D1"
    assert "Occupation; SOC code" in passages[0].text


def test_starts_sheet_fixture_shape_is_what_the_tests_assume() -> None:
    """Guards the fixture itself: the tests above encode its row numbers."""
    assert len(STARTS_SHEET) == 12
    assert STARTS_SHEET[4][0] == "Region"
    assert STARTS_SHEET[7] == ()


# --------------------------------------------------------------------------- #
# Layouts found in real ASIL workbooks
# --------------------------------------------------------------------------- #


def test_a_header_with_an_empty_corner_is_found(tmp_path: Path) -> None:
    """Regression, from FYA_vs_ASIL_Comparative_Analysis.xlsx sheet "Overview".

    The header sits in B and C with A blank, because column A holds row labels
    and has no name. It is one column narrower than every row beneath it, so a
    width test skips it and crowns the first data row — after which every row is
    captioned with a data value: ``Data source: Reference period``. Both rows end
    at the same column, which is what the right-edge test uses.
    """
    path = write_xlsx(tmp_path / "matrix.xlsx", {"Overview": MATRIX_SHEET})

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    by_range = {
        cell_locators([passage])[0].cell_range: passage.text for passage in passages
    }

    assert "FYA New Basics (2016): 4.2M job ads" in by_range["A3:C3"]
    assert "ASIL Research (2026): 950 job ads" in by_range["A3:C3"]
    # The unnamed row-label column keeps its letter rather than borrowing one.
    assert "Column A: Data source" in by_range["A3:C3"]
    # And emphatically not the old failure.
    assert "Data source: Reference period" not in by_range["A4:C4"]


def test_two_tables_on_one_sheet_each_get_their_own_header(tmp_path: Path) -> None:
    """Regression, from the same workbook's "Salary Premiums" sheet.

    A sheet is not a table. Applying table one's header to table two cites an
    average salary as a pay premium — a sound anchor, a plausible sentence, and
    the wrong column name.
    """
    path = write_xlsx(tmp_path / "two.xlsx", {"Salary": TWO_TABLE_SHEET})

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    by_range = {
        cell_locators([passage])[0].cell_range: passage.text for passage in passages
    }

    assert "Enterprise Skill: Presentation" in by_range["A3:C3"]
    assert "Pay Premium: $8,853" in by_range["A3:C3"]

    assert (
        "Industry Division: Professional, Scientific & Technical" in by_range["A8:C8"]
    )
    assert "Avg Salary Low: $109,860" in by_range["A8:C8"]
    assert "Pay Premium" not in by_range["A8:C8"]


def test_a_banner_row_separates_blocks_but_is_still_a_passage(tmp_path: Path) -> None:
    """Splitting must not drop the row it splits on."""
    path = write_xlsx(tmp_path / "two.xlsx", {"Salary": TWO_TABLE_SHEET})

    passages = extract(path, make_source(media_type=MediaType.XLSX))
    by_range = {
        cell_locators([passage])[0].cell_range: passage.text for passage in passages
    }

    assert by_range["A6"].endswith("ASIL (2026): Average Advertised Salary by Industry")


def test_split_tables_keeps_every_row(tmp_path: Path) -> None:
    """Whatever the blocking, the row count out equals the row count in."""
    rows = [
        TableRow(number=1, values={0: "banner"}),
        TableRow(number=2, values={0: "a", 1: "b"}),
        TableRow(number=3, values={0: "c", 1: "d"}),
        TableRow(number=4, values={0: "another banner"}),
        TableRow(number=5, values={0: "e", 1: "f"}),
    ]

    blocks = split_tables(rows)

    assert [row.number for block in blocks for row in block] == [1, 2, 3, 4, 5]
    assert [len(block) for block in blocks] == [1, 2, 1, 1]


def test_a_header_laid_out_over_two_lines_becomes_one_label(tmp_path: Path) -> None:
    """A real workbook header wraps: ``Enterprise Skill\n(ASIL framework)``.

    Carried into a passage the newline makes the row read as several lines and
    the label read as two, so a reviewer checking the citation sees something
    that does not look like what the passage says. DESIGN.md records this
    decision for cell text generally; it was implemented for DOCX only, and the
    XLSX and CSV path went through ``serialise_row`` uncollapsed.
    """
    path = write_xlsx(
        tmp_path / "wrapped.xlsx",
        {
            "Skill Demand": (
                ("Enterprise Skill\n(ASIL framework)", "ASIL 2026\n% of job ads"),
                ("Planning and Organising", "85%"),
            )
        },
    )

    passages = extract(path, make_source(media_type=MediaType.XLSX))

    for passage in passages:
        assert "\n" not in passage.text, passage.text
    data_row = passages[-1]
    assert "Enterprise Skill (ASIL framework): Planning and Organising" in data_row.text
    assert "ASIL 2026 % of job ads: 85%" in data_row.text


def test_the_real_workbook_has_no_newlines_in_any_passage() -> None:
    """The document the bug was found in. Its headers wrap in four columns."""
    workbook = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "sources"
        / "FYA_vs_ASIL_Comparative_Analysis.xlsx"
    )
    if not workbook.is_file():
        pytest.skip("no real ASIL workbook in data/sources")

    passages = extract(workbook, make_source(media_type=MediaType.XLSX))

    offenders = [passage.id for passage in passages if "\n" in passage.text]
    assert not offenders, f"{len(offenders)} passage(s) still carry newlines"
