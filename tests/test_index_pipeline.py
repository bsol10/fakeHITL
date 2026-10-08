"""The pipeline glue: registry to index, on generated files and on real ones.

``index_registry`` is where the stages meet, so it is where the failures that
span two stages show up — a source that extracts but does not chunk, an anchor
that survives ingest and is lost on the way into SQLite.

The real-document tests skip when ``data/sources`` is empty, so a checkout with
no client material still runs the suite. They are worth having anyway: every
chunking-adjacent bug this project has met came from a real file, and none from
a generated fixture. Client documents stay out of the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    PAGE_ONE,
    PAGE_TWO,
    StubEmbedder,
    make_source,
    registry_entry,
    write_docx,
    write_docx_with_table,
    write_pdf,
    write_registry,
)
from updater.models import BlockLocator, CellLocator, MediaType
from updater.provenance import derives_within
from updater.registry import load_registry
from updater.retrieve.chunk import chunk_passages
from updater.retrieve.index import index_registry, index_source
from updater.retrieve.search import DenseRetriever
from updater.retrieve.store import PassageStore
from updater.retrieve.tokenize import (
    TokenCounter,
    TokenizerUnavailable,
    default_token_counter,
)

SOURCES = Path(__file__).resolve().parent.parent / "data" / "sources"


def open_store(tmp_path: Path) -> PassageStore:
    return PassageStore(tmp_path / "index.sqlite3", dimensions=StubEmbedder.dimensions)


@pytest.fixture(scope="module")
def counter() -> TokenCounter:
    try:
        return default_token_counter()
    except TokenizerUnavailable as error:  # pragma: no cover - depends on install
        pytest.skip(f"embedding tokenizer unavailable: {error}")


def real_documents() -> list[Path]:
    if not SOURCES.is_dir():
        return []
    return sorted(
        path
        for path in SOURCES.iterdir()
        if path.is_file() and path.suffix.lower() in {".docx", ".xlsx"}
    )


# --------------------------------------------------------------------------- #
# Generated files, end to end
# --------------------------------------------------------------------------- #


def test_a_registered_document_goes_from_disk_to_searchable(
    tmp_path: Path, counter: TokenCounter
) -> None:
    write_docx_with_table(tmp_path / "report.docx")
    registry = load_registry(
        write_registry(
            tmp_path / "registry.yaml",
            [
                registry_entry(
                    url_or_path="report.docx",
                    media_type=MediaType.DOCX.value,
                )
            ],
        )
    )

    with open_store(tmp_path) as store:
        reports, failures = index_registry(
            registry, store=store, embedder=StubEmbedder(), counter=counter
        )

        assert failures == []
        assert len(reports) == 1
        assert reports[0].chunks > 0
        assert store.chunk_count() == reports[0].chunks

        results = DenseRetriever(store, StubEmbedder()).retrieve(
            "Industry: Manufacturing; Risk Group: Higher risk", top_k=3
        )
        assert results, "an indexed document should be retrievable"
        assert isinstance(results[0].passage.anchor.locator, BlockLocator | CellLocator)


def test_a_source_that_cannot_be_read_is_reported_not_skipped(
    tmp_path: Path, counter: TokenCounter
) -> None:
    """A missing file and an empty document are different facts, and the
    difference has to survive to the caller."""
    registry = load_registry(
        write_registry(
            tmp_path / "registry.yaml", [registry_entry(url_or_path="absent.docx")]
        )
    )

    with open_store(tmp_path) as store:
        reports, failures = index_registry(
            registry, store=store, embedder=StubEmbedder(), counter=counter
        )

    assert reports == []
    assert len(failures) == 1
    assert "no file there" in failures[0].reason


def test_one_bad_source_does_not_stop_the_others(
    tmp_path: Path, counter: TokenCounter
) -> None:
    write_docx(tmp_path / "good.docx")
    registry = load_registry(
        write_registry(
            tmp_path / "registry.yaml",
            [
                registry_entry(
                    id="good", url_or_path="good.docx", media_type=MediaType.DOCX.value
                ),
                registry_entry(id="missing", url_or_path="absent.docx"),
            ],
        )
    )

    with open_store(tmp_path) as store:
        reports, failures = index_registry(
            registry, store=store, embedder=StubEmbedder(), counter=counter
        )

    assert [report.source_id for report in reports] == ["good"]
    assert [failure.source_id for failure in failures] == ["missing"]


def test_a_pdf_page_chunk_never_reaches_into_the_next_page(
    tmp_path: Path, counter: TokenCounter
) -> None:
    """The rule BUILD_SPEC.md section 4 states in so many words, tested through
    the whole pipeline rather than on the chunker alone."""
    path = write_pdf(tmp_path / "report.pdf", [PAGE_ONE, PAGE_TWO])
    source = make_source(media_type=MediaType.PDF)

    with open_store(tmp_path) as store:
        index_source(
            source, path, store=store, embedder=StubEmbedder(), counter=counter
        )
        chunks = store.chunks()

    pages = {chunk.anchor.locator for chunk in chunks}
    assert len(pages) == 2, "expected chunks anchored to each of the two pages"
    for chunk in chunks:
        locator = chunk.anchor.locator
        assert not isinstance(locator, BlockLocator)
        page_text = " ".join(PAGE_ONE if getattr(locator, "page", 0) == 1 else PAGE_TWO)
        first_words = chunk.text.split()[0]
        assert first_words in page_text


def test_only_the_named_sources_are_indexed(
    tmp_path: Path, counter: TokenCounter
) -> None:
    write_docx(tmp_path / "one.docx")
    write_docx(tmp_path / "two.docx")
    registry = load_registry(
        write_registry(
            tmp_path / "registry.yaml",
            [
                registry_entry(
                    id="one", url_or_path="one.docx", media_type=MediaType.DOCX.value
                ),
                registry_entry(
                    id="two", url_or_path="two.docx", media_type=MediaType.DOCX.value
                ),
            ],
        )
    )

    with open_store(tmp_path) as store:
        reports, _ = index_registry(
            registry,
            store=store,
            embedder=StubEmbedder(),
            counter=counter,
            only={"two"},
        )

        assert [report.source_id for report in reports] == ["two"]
        assert store.source_ids() == ["two"]


# --------------------------------------------------------------------------- #
# Real ASIL documents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", real_documents(), ids=lambda path: path.stem[:40] if path else "none"
)
def test_chunking_a_real_document_keeps_every_anchor_inside_its_unit(
    path: Path, counter: TokenCounter
) -> None:
    """Run over the client's own material, which is where every chunking-adjacent
    bug in this project has come from."""
    from updater.ingest import extract

    source = make_source(source_id=path.stem, media_type=MediaType.DOCX)
    passages = extract(path, source)
    by_id = {passage.id: passage for passage in passages}

    chunks = chunk_passages(passages, counter=counter)

    assert chunks, f"{path.name} produced no chunks"
    for chunk in chunks:
        parent = by_id[chunk.id.rsplit("#", 1)[0]]
        assert derives_within(parent.anchor, chunk.anchor), (
            f"{chunk.id} escaped its parent"
        )
        assert chunk.anchor.locator == parent.anchor.locator
        offset = parent.anchor.char_start
        start = chunk.anchor.char_start - offset
        end = chunk.anchor.char_end - offset
        assert parent.text[start:end] == chunk.text


def test_a_known_real_row_survives_the_whole_pipeline(
    tmp_path: Path, counter: TokenCounter
) -> None:
    """The two-header workbook, indexed and retrieved.

    ``$109,860`` is an average salary that an earlier bug captioned as a pay
    premium. The label is part of the passage text, so this asserts through the
    index what the tabular tests assert at extraction: correct anchors are not
    enough, the column name has to be right too.
    """
    workbook = SOURCES / "FYA_vs_ASIL_Comparative_Analysis.xlsx"
    if not workbook.is_file():
        pytest.skip("no real ASIL workbook in data/sources")

    source = make_source(source_id="fya-vs-asil", media_type=MediaType.XLSX)
    with open_store(tmp_path) as store:
        index_source(
            source, workbook, store=store, embedder=StubEmbedder(), counter=counter
        )
        matching = [chunk for chunk in store.chunks() if "$109,860" in chunk.text]

    assert matching, "the salary row is missing from the index"
    salary_rows = [
        chunk
        for chunk in matching
        if isinstance(chunk.anchor.locator, CellLocator)
        and chunk.anchor.locator.sheet == "Salary Premiums"
    ]
    assert salary_rows, "expected the row from the Salary Premiums sheet"
    text = salary_rows[0].text
    assert "Avg Salary Low (AUD): $109,860" in text, text
    assert "Pay Premium" not in text, "the second table's header leaked into the first"
