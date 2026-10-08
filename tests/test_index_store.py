"""Embedding, storage and retrieval.

Three things are under test here, and only the first is about search quality:

*No text is silently unembedded.* The model's window is 256 word pieces and a
chunk may be 600, so a chunk has to be embedded as several windows. If that were
wrong the index would look complete, contain every chunk, and be searchable only
on the first 40% of the long ones. Nothing would report it.

*A chunk comes back out exactly as it went in.* Text, token count, locator
variant, offsets, timestamp. An anchor that survives storage in a slightly
different form is not provenance any more.

*Re-indexing replaces.* Running twice must not leave two copies of a source's
chunks in the index, one of them describing a document that no longer says that.
The full-text index is held to the same standard: a word that has left the
document must stop finding it, and must never find the chunk that replaced it.

The embedder here is a hash-based stub. Loading the real model takes twelve
seconds and none of the properties above are properties of the model.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import (
    StubEmbedder,
    WordCounter,
    make_passage,
    make_source,
    sentinel_text,
)
from updater.models import (
    BlockLocator,
    CellLocator,
    DomLocator,
    Locator,
    PageLocator,
    Passage,
)
from updater.retrieve.chunk import ChunkConfig, chunk_passage
from updater.retrieve.embed import (
    USABLE_WINDOW_TOKENS,
    embed_chunks,
    window_spans,
)
from updater.retrieve.search import DenseRetriever
from updater.retrieve.store import (
    IncompatibleIndex,
    PassageStore,
    StoreError,
    match_expression,
)
from updater.retrieve.tokenize import TokenizerUnavailable, default_token_counter


def open_store(tmp_path: Path, **kwargs: object) -> PassageStore:
    """A store with the stub embedder's geometry rather than the model's."""
    settings: dict[str, object] = {"dimensions": StubEmbedder.dimensions}
    settings.update(kwargs)
    return PassageStore(tmp_path / "index.sqlite3", **settings)  # type: ignore[arg-type]


def indexed(store: PassageStore, chunks: list[Passage]) -> None:
    source = make_source(source_id=chunks[0].source_id)
    windows = embed_chunks(chunks, embedder=StubEmbedder(), counter=WordCounter())
    store.replace_source(source, chunks, windows)


def rows(*texts: str) -> list[Passage]:
    """One table row per text, each with its own id and cell range."""
    return [
        make_passage(
            text,
            passage_id=f"src:row{index}#0",
            locator=CellLocator(sheet="Sheet1", cell_range=f"A{index}:D{index}"),
        )
        for index, text in enumerate(texts, start=2)
    ]


def schema_of(path: Path) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        connection.close()


def assert_text_index_matches_chunks(path: Path) -> None:
    """FTS5's own check that the full-text index agrees with ``chunks``.

    With ``rank = 1`` it compares the index against the external content table
    rather than only against itself. It raises when they disagree.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)"
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Windowing — the silent-truncation guard
# --------------------------------------------------------------------------- #


def test_a_chunk_within_the_window_is_embedded_whole() -> None:
    chunk = make_passage("Sustained destinations rose to 68% of the cohort.")

    windows = embed_chunks([chunk], embedder=StubEmbedder(), counter=WordCounter())

    assert len(windows) == 1
    assert (windows[0].char_start, windows[0].char_end) == (0, len(chunk.text))


def test_a_chunk_longer_than_the_window_is_embedded_in_several() -> None:
    """The property that keeps a 600-token chunk fully searchable. Without it
    sentence-transformers truncates at 256 word pieces and says nothing."""
    try:
        counter = default_token_counter()
    except TokenizerUnavailable as error:  # pragma: no cover - depends on install
        pytest.skip(f"embedding tokenizer unavailable: {error}")
    long_text = sentinel_text(count=40)
    assert counter.count(long_text) > USABLE_WINDOW_TOKENS

    spans = window_spans(long_text, counter=counter)

    assert len(spans) > 1
    for start, end in spans:
        assert counter.count(long_text[start:end]) <= USABLE_WINDOW_TOKENS
    assert spans[0][0] == 0
    assert spans[-1][1] == len(long_text)


def test_windows_carry_their_chunks_id_and_their_own_ordinals() -> None:
    chunk = make_passage(sentinel_text(count=20))
    counter = WordCounter()

    windows = embed_chunks(
        [chunk],
        embedder=StubEmbedder(),
        counter=counter,
    )

    assert {window.chunk_id for window in windows} == {chunk.id}
    assert [window.ordinal for window in windows] == list(range(len(windows)))
    assert all(len(window.vector) == StubEmbedder.dimensions for window in windows)


def test_identical_text_embeds_identically() -> None:
    """A sanity check on the stub itself: the store tests below assume a query
    for a chunk's own text finds that chunk."""
    embedder = StubEmbedder()

    first, second = embedder.encode(["North East", "North East"])

    assert first == second


# --------------------------------------------------------------------------- #
# Storage round trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "locator",
    [
        PageLocator(page=14),
        BlockLocator(paragraph_index=194, style=""),
        CellLocator(sheet="Salary Premiums", cell_range="A13:D13"),
        DomLocator(css_path="doc > main > p", heading_trail=("Method",)),
    ],
)
def test_a_chunk_comes_back_exactly_as_it_went_in(
    tmp_path: Path, locator: Locator
) -> None:
    chunk = make_passage(
        "Region: North East; Starts 2024: 11845",
        passage_id="src:cell#0",
        locator=locator,
    )

    with open_store(tmp_path) as store:
        indexed(store, [chunk])
        restored = store.get_chunk("src:cell#0")

    assert restored == chunk


def test_offsets_survive_storage(tmp_path: Path) -> None:
    """A chunk from the middle of a page carries non-zero offsets, and those are
    what a reviewer uses to find the text. Storing them as anything other than
    what they were is a wrong citation."""
    parent = make_passage(sentinel_text())
    chunk = chunk_passage(
        parent, counter=WordCounter(), config=ChunkConfig(40, 25, 0.15)
    )[2]
    assert chunk.anchor.char_start > 0

    with open_store(tmp_path) as store:
        indexed(store, [chunk])
        restored = store.get_chunk(chunk.id)

    assert restored.anchor.char_start == chunk.anchor.char_start
    assert restored.anchor.char_end == chunk.anchor.char_end
    assert restored.anchor.extracted_at == chunk.anchor.extracted_at


def test_re_indexing_replaces_rather_than_appends(tmp_path: Path) -> None:
    """Two copies of a source's chunks, one of them stale, is the worst failure
    this store could have: both would be retrievable and only one is true."""
    chunks = chunk_passage(
        make_passage(sentinel_text()),
        counter=WordCounter(),
        config=ChunkConfig(40, 25, 0.15),
    )

    with open_store(tmp_path) as store:
        indexed(store, chunks)
        first_count = store.chunk_count()
        first_windows = store.window_count()
        indexed(store, chunks)

        assert store.chunk_count() == first_count
        assert store.window_count() == first_windows


def test_re_indexing_drops_the_old_vectors_too(tmp_path: Path) -> None:
    """The vec0 table is virtual, so ON DELETE CASCADE does not reach it. If its
    rows were left behind, a re-indexed source would retrieve nothing: the join
    to ``windows`` would drop every orphaned vector silently."""
    config = ChunkConfig(40, 25, 0.15)
    counter = WordCounter()
    original = chunk_passage(
        make_passage(sentinel_text()), counter=counter, config=config
    )
    shorter = chunk_passage(
        make_passage(sentinel_text(count=6)), counter=counter, config=config
    )

    with open_store(tmp_path) as store:
        indexed(store, original)
        indexed(store, shorter)

        assert store.chunk_count() == len(shorter)
        assert store.window_count() == len(shorter)
        results = DenseRetriever(store, StubEmbedder()).retrieve(
            shorter[0].text, top_k=50
        )
        assert len(results) <= len(shorter), "a stale vector survived re-indexing"


def test_windows_referencing_unknown_chunks_are_refused(tmp_path: Path) -> None:
    chunk = make_passage("A row of a table.", passage_id="src:a#0")
    windows = embed_chunks([chunk], embedder=StubEmbedder(), counter=WordCounter())
    orphan = [replace(windows[0], chunk_id="src:does-not-exist#0")]

    with open_store(tmp_path) as store, pytest.raises(StoreError, match="not supplied"):
        store.replace_source(make_source(source_id="src"), [chunk], orphan)


def test_a_chunk_from_another_source_is_refused(tmp_path: Path) -> None:
    chunk = make_passage("A row.", source_id="other-source", passage_id="other:a#0")
    windows = embed_chunks([chunk], embedder=StubEmbedder(), counter=WordCounter())

    with open_store(tmp_path) as store, pytest.raises(StoreError, match="belongs to"):
        store.replace_source(make_source(source_id="src"), [chunk], windows)


def test_the_source_is_stored_with_its_registry_metadata(tmp_path: Path) -> None:
    """``updater search`` renders citations with the source *title*, which it
    reads back from here rather than reloading the registry."""
    chunk = make_passage("A row.", source_id="dfe-destinations-2025")

    with open_store(tmp_path) as store:
        indexed(store, [chunk])
        source = store.get_source("dfe-destinations-2025")

    assert source.title == "Destinations of KS4 and KS5 students"
    assert source.owner == "Department for Education"


def test_locator_kinds_summarises_the_index(tmp_path: Path) -> None:
    chunks = [
        make_passage("Page text.", passage_id="src:p1#0", locator=PageLocator(page=1)),
        make_passage(
            "Row text.",
            passage_id="src:c1#0",
            locator=CellLocator(sheet="S", cell_range="A1"),
        ),
    ]

    with open_store(tmp_path) as store:
        indexed(store, chunks)

        assert store.locator_kinds() == {"cell": 1, "page": 1}


# --------------------------------------------------------------------------- #
# Index settings
# --------------------------------------------------------------------------- #


def test_an_index_built_with_another_model_is_refused(tmp_path: Path) -> None:
    """Vectors from two models in one table are not comparable, and the symptom
    is retrieval that is quietly worse rather than obviously broken."""
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("A row.")])

    with pytest.raises(IncompatibleIndex, match="embedding_model"):
        open_store(tmp_path, model_id="some-other/model")


def test_an_index_built_with_a_different_chunk_budget_is_refused(
    tmp_path: Path,
) -> None:
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("A row.")])

    with pytest.raises(IncompatibleIndex, match="chunk"):
        open_store(tmp_path, config=ChunkConfig(max_tokens=300, min_tokens=200))


def make_version_1_index(tmp_path: Path) -> Path:
    """An index as schema version 1 left it: no full-text table."""
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("Apprenticeship starts fell.")])
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    with connection:
        for trigger in ("insert", "delete", "update"):
            connection.execute(f"DROP TRIGGER chunks_fts_{trigger}")
        connection.execute("DROP TABLE chunks_fts")
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.close()
    return path


def test_an_index_with_an_older_schema_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    """The settings are checked before any table is created. Checking after
    would add version 2's tables to a file it then refuses, half-upgrading an
    index that nobody asked to change."""
    path = make_version_1_index(tmp_path)
    before = schema_of(path)

    with pytest.raises(IncompatibleIndex, match="schema_version"):
        open_store(tmp_path)

    assert schema_of(path) == before


def test_rebuild_opens_an_index_the_settings_check_refuses(tmp_path: Path) -> None:
    """The refusal tells the operator to run ``updater ingest --rebuild``, so
    the rebuild has to get past the same refusal — and the only other way past
    it is deleting a file that also holds the audit log."""
    make_version_1_index(tmp_path)

    with open_store(tmp_path, rebuild=True) as store:
        assert store.chunk_count() == 0
        indexed(store, rows("Region: North East; Starts 2024: 11845"))
        assert [r.passage.id for r in store.search_text("11845")] == ["src:row2#0"]

    with open_store(tmp_path) as reopened:
        assert reopened.chunk_count() == 1


def test_rebuild_empties_the_index(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("Apprenticeship starts fell.")])

    with open_store(tmp_path, rebuild=True) as store:
        assert store.chunk_count() == 0
        assert store.window_count() == 0
        assert store.source_ids() == []
        assert store.search_text("apprenticeship") == []
    assert_text_index_matches_chunks(tmp_path / "index.sqlite3")


def test_a_query_vector_of_the_wrong_width_is_refused(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("A row.")])

        with pytest.raises(StoreError, match="dimensions"):
            store.search([0.1, 0.2, 0.3])


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


def test_a_chunk_is_retrieved_by_its_own_text(tmp_path: Path) -> None:
    chunks = [
        make_passage(
            f"Region: {name}; Starts 2024: {number}",
            passage_id=f"src:row{index}#0",
            locator=CellLocator(sheet="Starts", cell_range=f"A{index}:D{index}"),
        )
        for index, (name, number) in enumerate(
            [("North East", 11845), ("London", 43220), ("Mining", 55)], start=2
        )
    ]

    with open_store(tmp_path) as store:
        indexed(store, chunks)
        results = DenseRetriever(store, StubEmbedder()).retrieve(
            chunks[1].text, top_k=3
        )

    assert results[0].passage.id == "src:row3#0"
    assert results[0].retriever == "dense"
    assert results[0].score == pytest.approx(1.0, abs=1e-6)


def test_results_carry_the_anchor_not_just_the_text(tmp_path: Path) -> None:
    """Rule 1: there is no stage where text becomes a bare string. Retrieval is
    the stage where it would be most tempting."""
    chunk = make_passage(
        "Region: North East",
        locator=CellLocator(sheet="Apprenticeship starts", cell_range="A6:D6"),
    )

    with open_store(tmp_path) as store:
        indexed(store, [chunk])
        result = DenseRetriever(store, StubEmbedder()).retrieve(chunk.text)[0]

    assert result.passage.anchor.locator == CellLocator(
        sheet="Apprenticeship starts", cell_range="A6:D6"
    )
    assert result.passage.anchor.source_id == chunk.source_id


def test_top_k_limits_the_results(tmp_path: Path) -> None:
    chunks = [
        make_passage(f"Row number {index}.", passage_id=f"src:r{index}#0")
        for index in range(12)
    ]

    with open_store(tmp_path) as store:
        indexed(store, chunks)
        results = DenseRetriever(store, StubEmbedder()).retrieve(
            "Row number 4.", top_k=3
        )

    assert len(results) == 3


def test_a_long_chunk_is_returned_once_not_once_per_window(tmp_path: Path) -> None:
    """Several windows point at one chunk, so the search collapses them by best
    score. Without that, one long chunk could fill the whole result list."""
    long_chunk = make_passage(sentinel_text(count=20), passage_id="src:long#0")
    other = make_passage("An unrelated row.", passage_id="src:other#0")

    with open_store(tmp_path) as store:
        source = make_source(source_id="src")
        chunks = [long_chunk, other]
        windows = embed_chunks(chunks, embedder=StubEmbedder(), counter=WordCounter())
        store.replace_source(source, chunks, windows)
        assert store.window_count() > 2

        results = DenseRetriever(store, StubEmbedder()).retrieve(
            long_chunk.text[:200], top_k=5
        )

    assert [result.passage.id for result in results].count("src:long#0") == 1


def test_an_empty_query_retrieves_nothing(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("A row.")])

        assert DenseRetriever(store, StubEmbedder()).retrieve("   ") == []


def test_the_index_is_a_file_that_survives_reopening(tmp_path: Path) -> None:
    chunk = make_passage("Region: North East", passage_id="src:a#0")

    with open_store(tmp_path) as store:
        indexed(store, [chunk])
    with open_store(tmp_path) as reopened:
        assert reopened.chunk_count() == 1
        assert reopened.get_chunk("src:a#0") == chunk


def test_extracted_at_is_preserved_to_the_second(tmp_path: Path) -> None:
    stamped = datetime(2026, 9, 7, 12, 30, 45, tzinfo=UTC)
    chunk = replace(
        make_passage("A row."),
        anchor=replace(make_passage("A row.").anchor, extracted_at=stamped),
    )

    with open_store(tmp_path) as store:
        indexed(store, [chunk])

        assert store.get_chunk(chunk.id).anchor.extracted_at == stamped


# --------------------------------------------------------------------------- #
# The full-text index
# --------------------------------------------------------------------------- #


def test_text_search_finds_a_chunk_by_a_word_in_it(tmp_path: Path) -> None:
    chunks = rows(
        "Industry: Mining; Starts 2024: 55",
        "Industry: Construction; Starts 2024: 8120",
    )

    with open_store(tmp_path) as store:
        indexed(store, chunks)
        results = store.search_text("mining")

    assert [result.passage for result in results] == [chunks[0]]


def test_a_better_text_match_ranks_first_and_scores_higher(tmp_path: Path) -> None:
    """BM25 comes out of SQLite lower-is-better. Negating it makes it read like
    every other score in the package; this pins the sign as well as the order.
    The filler rows are there because BM25's IDF is meaningless over two."""
    both, one, *filler = rows(
        "Salary Premiums. Pay Premium (AUD/year): $12,400",
        "Pay band: Grade 4; Avg Salary Low (AUD): $109,860",
        "Region: North East; Starts 2024: 11845",
        "Region: London; Starts 2024: 43220",
        "Industry: Mining; Starts 2024: 55",
    )

    with open_store(tmp_path) as store:
        indexed(store, [one, both, *filler])
        results = store.search_text("pay premium")

    assert [result.passage.id for result in results] == [both.id, one.id]
    assert results[0].score > results[1].score


def test_a_figure_is_searched_as_a_phrase(tmp_path: Path) -> None:
    """The tokenizer splits 109,860 at the comma. Searched as two separate
    words, it would also find a row holding 860 in one column and 109 in
    another — a citation for a figure the row does not state."""
    salary, scattered = rows(
        "Avg Salary Low (AUD): $109,860; Avg Salary High (AUD): $131,200",
        "Providers: 860; Programs: 109",
    )

    with open_store(tmp_path) as store:
        indexed(store, [salary, scattered])
        results = store.search_text("109,860")

    assert [result.passage.id for result in results] == [salary.id]


@pytest.mark.parametrize(
    "query",
    [
        "AND",
        "risk AND",
        "OR NOT",
        "NEAR(salary premium)",
        "risk*",
        "^risk",
        "text:risk",
        "-risk",
        '"unbalanced',
        'a"b',
        "it's",
        "( )",
        "' OR 1=1 --",
        "risk\x00premium",
        "\x00",
        "* ^ :",
        "",
    ],
)
def test_query_syntax_in_a_query_is_searched_for_not_parsed(
    tmp_path: Path, query: str
) -> None:
    """FTS5 reserves AND, OR, NOT, NEAR, *, ^ and :, and a NUL ends one of its
    strings early. Someone pasting a sentence into the search box is not
    writing FTS5, and a syntax error is the wrong answer to give them."""
    with open_store(tmp_path) as store:
        indexed(store, rows("Risk Group: Higher risk; Premium: 4%"))

        store.search_text(query)


def test_an_operator_is_searched_for_as_a_word(tmp_path: Path) -> None:
    chunks = rows("Prior value: Not in last year's report", "Region: London")

    with open_store(tmp_path) as store:
        indexed(store, chunks)

        assert [r.passage for r in store.search_text("NOT")] == [chunks[0]]


def test_a_query_with_nothing_searchable_finds_nothing() -> None:
    assert match_expression("* ^ : -- ()") is None
    assert match_expression("") is None


def test_re_indexing_replaces_the_text_index_too(tmp_path: Path) -> None:
    """The failure this guards against is worse than a stale result. Rowids are
    reused once the rows holding them are deleted, so a full-text entry left
    behind for the old text would point at the *new* chunk, and a search for a
    word the document no longer contains would return a passage that does not
    contain it, with a sound anchor."""
    with open_store(tmp_path) as store:
        indexed(store, [make_passage("Apprenticeship starts fell.", passage_id="a")])
        indexed(store, [make_passage("Traineeship completions rose.", passage_id="a")])

        assert store.search_text("apprenticeship") == []
        assert [r.passage.text for r in store.search_text("traineeship")] == [
            "Traineeship completions rose."
        ]
    assert_text_index_matches_chunks(tmp_path / "index.sqlite3")


def test_every_text_hit_contains_what_was_searched_for(tmp_path: Path) -> None:
    """Two sources, one re-indexed twice: whatever the rowids did in between,
    each hit must contain the word it was found by."""
    with open_store(tmp_path) as store:
        indexed(store, rows("Region: North East", "Region: London"))
        indexed(store, [make_passage("Region: Wales", source_id="other")])
        indexed(store, rows("Region: Scotland"))
        indexed(store, rows("Region: Yorkshire", "Region: London"))

        for word in ("north", "london", "wales", "scotland", "yorkshire"):
            for result in store.search_text(word):
                assert word in result.passage.text.lower(), (word, result.passage)
    assert_text_index_matches_chunks(tmp_path / "index.sqlite3")


def test_chunk_rowids_are_declared_so_vacuum_cannot_renumber_them(
    tmp_path: Path,
) -> None:
    """The full-text index stores rowids, and the join turns them back into
    chunks. SQLite documents that ``VACUUM`` may renumber the rowids of a table
    without an ``INTEGER PRIMARY KEY``, after which every text hit would resolve
    to the wrong chunk. SQLite 3.53 happens not to, so no behavioural test can
    show the failure here; declaring the column is the guarantee, and this pins
    it."""
    with open_store(tmp_path):
        pass
    connection = sqlite3.connect(tmp_path / "index.sqlite3")
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(chunks)")}
    connection.close()

    _, _, declared_type, _, _, primary_key = columns["rowid"]
    assert (declared_type, primary_key) == ("INTEGER", 1)
