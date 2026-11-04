"""Hybrid retrieval: full-text search and dense, fused by reciprocal rank.

The first test reproduces the shape found on ASIL's real index: the row stating
``109,860`` ranks outside dense retrieval's top 50, which is as far as fusion
reads. It and an unrelated header each rank first in one list and tie at
exactly 1/61, so only the tie-break decides which a reviewer sees first.

Dense rankings here are scripted, not hashed: a test about fusing two rankings
has to control both, and the stub embedder's hash geometry would make the dense
half an accident.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from conftest import StubEmbedder, WordCounter, make_passage, make_source
from updater.models import CellLocator, Passage
from updater.retrieve.embed import embed_chunks
from updater.retrieve.search import (
    DenseRetriever,
    HybridRetriever,
    RetrievalResult,
    SparseRetriever,
    reciprocal_rank_fusion,
)
from updater.retrieve.store import PassageStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class ScriptedEmbedder:
    """The query on the first axis, every other text at a chosen cosine to it.

    Texts are given in the order dense retrieval should rank them, closest
    first, and each sits at its own similarity so no two tie.
    """

    dimensions = StubEmbedder.dimensions

    def __init__(self, query: str, texts_closest_first: Sequence[str]) -> None:
        self.query = query
        self.closeness = {
            text: 0.95 - 0.01 * position
            for position, text in enumerate(texts_closest_first)
        }

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        padding = [0.0] * (self.dimensions - 2)
        vectors = []
        for text in texts:
            similarity = 1.0 if text == self.query else self.closeness[text]
            vectors.append([similarity, math.sqrt(1 - similarity**2), *padding])
        return vectors


def indexed_in_dense_order(
    tmp_path: Path, query: str, texts_closest_first: Sequence[str]
) -> tuple[PassageStore, HybridRetriever, DenseRetriever]:
    """An index whose dense ranking for ``query`` is the order given."""
    chunks = [
        make_passage(
            text,
            passage_id=f"src:row{index}#0",
            locator=CellLocator(sheet="Sheet1", cell_range=f"A{index}:D{index}"),
        )
        for index, text in enumerate(texts_closest_first, start=2)
    ]
    embedder = ScriptedEmbedder(query, texts_closest_first)
    store = PassageStore(tmp_path / "index.sqlite3", dimensions=embedder.dimensions)
    windows = embed_chunks(chunks, embedder=embedder, counter=WordCounter())
    store.replace_source(make_source(source_id="src"), chunks, windows)
    dense = DenseRetriever(store, embedder)
    return store, HybridRetriever(dense, SparseRetriever(store)), dense


def filler(count: int) -> list[str]:
    """Rows sharing no word with any query below."""
    return [f"Provider {n}. Completions 2024: {1000 + n}" for n in range(count)]


# The figure dense retrieval cannot find


def test_a_figure_dense_ranks_outside_its_top_50_comes_first(tmp_path: Path) -> None:
    salary = (
        "Salary Premiums. Enterprise Skill: Leadership; "
        "Avg Salary Low (AUD): $109,860; Avg Salary High (AUD): $131,200"
    )
    header = (
        "Table 16. Enterprise Skill; Employer Demand (% of job ads); How ASIL builds it"
    )
    store, hybrid, dense = indexed_in_dense_order(
        tmp_path, "109,860", [header, *filler(54), salary]
    )
    with store:
        dense_only = dense.retrieve("109,860", top_k=60)
        results = hybrid.retrieve("109,860", top_k=5)

    # What dense alone does with a number, which is the bug.
    assert "109,860" not in dense_only[0].passage.text
    assert [r.passage.text for r in dense_only].index(salary) + 1 == 56
    assert [r.dense_rank for r in dense_only] == list(range(1, 57))

    assert results[0].passage.text == salary
    assert (results[0].dense_rank, results[0].sparse_rank) == (None, 1)
    # The tie it won: each was rank 1 in one list and absent from the other.
    assert results[1].passage.text == header
    assert results[0].score == results[1].score == 1 / 61


def test_fusion_reads_each_retrievers_top_50_not_top_k(tmp_path: Path) -> None:
    """A chunk both retrievers rank second (2/62) outscores one only dense or
    only full-text search ranks first (1/61). Asked for one result, fusion that
    read each list only as far as ``top_k`` would never see the second places."""
    closest_with_no_words = "Industry: Mining; Completions 2024: 55"
    agreed = "Apprenticeship completions by region"
    lexical_only = "Apprenticeship starts: 12,340; apprenticeship starts rose"
    store, hybrid, _ = indexed_in_dense_order(
        tmp_path,
        "apprenticeship starts",
        [closest_with_no_words, agreed, *filler(55), lexical_only],
    )
    with store:
        best = hybrid.retrieve("apprenticeship starts", top_k=1)

    assert [r.passage.text for r in best] == [agreed]
    assert (best[0].dense_rank, best[0].sparse_rank) == (2, 2)


# Prose is not regressed


def test_an_answer_only_dense_can_find_is_still_returned(tmp_path: Path) -> None:
    """A paraphrase shares no word with the passage that answers it, and the
    words it does share pull other rows up. Fusion may move the answer down the
    list; it must not lose it from the five a reviewer reads."""
    query = "which jobs are most likely to be replaced by machines"
    answer = "Higher automation risk industries (9 industries)"
    store, hybrid, _ = indexed_in_dense_order(
        tmp_path,
        query,
        [
            answer,
            "Jobs of the Future. Most in-demand enterprise skills",
            "Machines and equipment operators: 4% of job ads",
            "Jobs most likely to grow: care, education, construction",
            *filler(3),
        ],
    )
    with store:
        results = hybrid.retrieve(query, top_k=5)

    found = [r for r in results if r.passage.text == answer]
    assert found, [r.passage.text for r in results]
    assert (found[0].dense_rank, found[0].sparse_rank) == (1, None)


def test_a_query_with_query_syntax_in_it_does_not_raise(tmp_path: Path) -> None:
    query = 'NOT AND OR NEAR(risk) * ^ : "risk'
    store, hybrid, _ = indexed_in_dense_order(
        tmp_path, query, ["Risk Group: Higher risk", "Region: London"]
    )
    with store:
        results = hybrid.retrieve(query)

    assert results[0].passage.text == "Risk Group: Higher risk"


def test_an_empty_query_retrieves_nothing(tmp_path: Path) -> None:
    store, hybrid, _ = indexed_in_dense_order(tmp_path, "   ", ["Region: London"])
    with store:
        assert hybrid.retrieve("   ") == []


# The fusion arithmetic


def ranked(*ids: str) -> list[RetrievalResult]:
    return [
        RetrievalResult(passage=passage(chunk_id), score=0.0, retriever="test")
        for chunk_id in ids
    ]


def passage(chunk_id: str) -> Passage:
    return make_passage(f"Row {chunk_id}.", passage_id=chunk_id)


def test_each_list_contributes_one_over_k_plus_rank_counted_from_one() -> None:
    fused = reciprocal_rank_fusion(ranked("a", "b"), ranked("b", "c"))
    by_id = {result.passage.id: result for result in fused}

    assert [result.passage.id for result in fused] == ["b", "a", "c"]
    assert by_id["b"].score == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["a"].score == pytest.approx(1 / 61)
    assert by_id["c"].score == pytest.approx(1 / 62)
    assert (by_id["b"].dense_rank, by_id["b"].sparse_rank) == (2, 1)
    assert (by_id["a"].dense_rank, by_id["a"].sparse_rank) == (1, None)
    assert (by_id["c"].dense_rank, by_id["c"].sparse_rank) == (None, 2)
    assert {result.retriever for result in fused} == {"hybrid"}


def test_a_tie_goes_to_the_better_lexical_rank() -> None:
    """Ids are chosen so that alphabetical order, insertion order and dense
    order all disagree with the rule; only the lexical rank picks the winner."""
    alone = reciprocal_rank_fusion(ranked("a-dense"), ranked("z-sparse"))
    assert [r.passage.id for r in alone] == ["z-sparse", "a-dense"]

    # Both in both lists: dense 2 + sparse 5 against dense 5 + sparse 2.
    both = reciprocal_rank_fusion(
        ranked("d1", "a-x", "d3", "d4", "z-y"),
        ranked("s1", "z-y", "s3", "s4", "a-x"),
    )
    order = [r.passage.id for r in both]
    assert both[order.index("a-x")].score == both[order.index("z-y")].score
    assert order.index("z-y") < order.index("a-x")
