"""Retrieval over the chunk index: dense, sparse (full-text BM25), and the two fused.

Dense retrieval finds prose but not figures: ``109,860`` is a few word pieces
with no meaning, and its nearest neighbours are other numbers. Full-text search
finds it first time, so retrieval is hybrid (BUILD_SPEC.md section 4).
:class:`Retriever` is a protocol, and fusion is a function over two retrievers'
results rather than a change to either.

*Fusion is reciprocal rank fusion, over ranks, never scores.* Cosine and BM25
share no scale, and normalising one to look like the other is a guess. A chunk
scores ``1 / (k + rank)`` from each list it is in, with ``k = 60`` and each list
read to its top 50 (BACKEND_PLAN.md phase 2).

*Equal fused scores go to the better full-text rank.* A chunk only dense found
at rank 1 and one only full-text found at rank 1 both score exactly 1/61. A
lexical hit is checkable (the query's words are in the text), while a dense rank
for a number is noise. The rule only orders what RRF scores equally.

*The fused score is not a relevance measure.* It depends only on ranks, so the
first result scores the same whether it is an exact match or the best of a bad
lot. Whether retrieval "came back thin" (the compare stage's ``cannot-verify``)
has to be read from the inputs, not from this number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from updater.retrieve.embed import SentenceTransformerEmbedder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from updater.models import Passage
    from updater.retrieve.embed import Embedder
    from updater.retrieve.store import PassageStore

__all__ = [
    "FUSION_DEPTH",
    "RRF_K",
    "DenseRetriever",
    "HybridRetriever",
    "RetrievalResult",
    "Retriever",
    "SparseRetriever",
    "reciprocal_rank_fusion",
]

RRF_K = 60
"""The constant in ``1 / (k + rank)``. Larger flattens the gap between ranks."""

FUSION_DEPTH = 50
"""How far down each retriever's list fusion reads, whatever ``top_k`` is asked
for. A chunk both retrievers place second outscores one only dense places first,
and reading each list only as far as ``top_k`` would never see it."""


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One chunk a retriever thinks is relevant, and how it says so.

    ``retriever`` and the two input ranks answer "why did this come back?",
    which a fused list cannot otherwise answer.
    """

    passage: Passage
    score: float
    retriever: str
    window_ordinal: int = 0
    """Which embedding window matched best; meaningless without a dense rank."""
    dense_rank: int | None = None
    """1-based rank in the dense results; None when dense did not return it."""
    sparse_rank: int | None = None
    """1-based rank in the full-text results; None when they did not return it."""

    @property
    def anchor_summary(self) -> str:
        """A terse rendering of where this came from, for logs. Not a citation:
        :func:`updater.citations.render_anchor` renders those."""
        locator = self.passage.anchor.locator
        return f"{self.passage.source_id} {locator}"


class Retriever(Protocol):
    """Turns a query into ranked chunks. Dense, sparse, or a fusion of both."""

    name: str

    def retrieve(self, query: str, *, top_k: int = 10) -> list[RetrievalResult]: ...


class DenseRetriever:
    """Nearest neighbours in embedding space, best window per chunk.

    Scores are cosine similarities, comparable across queries, which is what
    lets the compare stage set a threshold for retrieval that "came back thin".
    """

    name = "dense"

    def __init__(self, store: PassageStore, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder or SentenceTransformerEmbedder()

    def retrieve(self, query: str, *, top_k: int = 10) -> list[RetrievalResult]:
        if not query.strip():
            return []
        vectors = self.embedder.encode([query])
        if not vectors:
            return []
        return [
            RetrievalResult(
                passage=scored.passage,
                score=scored.score,
                retriever=self.name,
                window_ordinal=scored.window_ordinal,
                dense_rank=rank,
            )
            for rank, scored in enumerate(
                self.store.search(vectors[0], top_k=top_k), start=1
            )
        ]


class SparseRetriever:
    """Full-text search over the chunk text, ranked by BM25.

    The retriever that can find a number. The query is never parsed as FTS5
    syntax, so it cannot raise. Scores are BM25, higher better, and mean nothing
    outside the query that produced them.
    """

    name = "sparse"

    def __init__(self, store: PassageStore) -> None:
        self.store = store

    def retrieve(self, query: str, *, top_k: int = 10) -> list[RetrievalResult]:
        return [
            RetrievalResult(
                passage=scored.passage,
                score=scored.score,
                retriever=self.name,
                sparse_rank=rank,
            )
            for rank, scored in enumerate(
                self.store.search_text(query, top_k=top_k), start=1
            )
        ]


def reciprocal_rank_fusion(
    dense: Sequence[RetrievalResult],
    sparse: Sequence[RetrievalResult],
    *,
    k: int = RRF_K,
) -> list[RetrievalResult]:
    """Merge two ranked lists into one, by rank alone.

    Each chunk scores ``1 / (k + rank)`` from each list it is in, ranks counted
    from 1 by position. Equal scores are ordered by the better full-text rank,
    then the better dense rank, then chunk id, so the same inputs always give
    the same order — see the module docstring for why lexical goes first.
    """
    first_seen: dict[str, RetrievalResult] = {}
    dense_ranks: dict[str, int] = {}
    sparse_ranks: dict[str, int] = {}
    for results, ranks in ((dense, dense_ranks), (sparse, sparse_ranks)):
        for rank, result in enumerate(results, start=1):
            ranks.setdefault(result.passage.id, rank)
            first_seen.setdefault(result.passage.id, result)

    fused = []
    for chunk_id, result in first_seen.items():
        dense_rank = dense_ranks.get(chunk_id)
        sparse_rank = sparse_ranks.get(chunk_id)
        fused.append(
            RetrievalResult(
                passage=result.passage,
                score=sum(
                    1 / (k + rank)
                    for rank in (dense_rank, sparse_rank)
                    if rank is not None
                ),
                retriever=HybridRetriever.name,
                # Dense is read first, so this is its window whenever it has one.
                window_ordinal=result.window_ordinal,
                dense_rank=dense_rank,
                sparse_rank=sparse_rank,
            )
        )
    return sorted(fused, key=_fused_order)


def _fused_order(result: RetrievalResult) -> tuple[float, float, float, str]:
    def rank(value: int | None) -> float:
        return math.inf if value is None else value

    return (
        -result.score,
        rank(result.sparse_rank),
        rank(result.dense_rank),
        result.passage.id,
    )


class HybridRetriever:
    """Dense and full-text retrieval, fused with :func:`reciprocal_rank_fusion`.

    Both retrievers are read to :data:`FUSION_DEPTH` whatever ``top_k`` is, and
    the fused list is cut to ``top_k`` afterwards.
    """

    name = "hybrid"

    def __init__(
        self,
        dense: Retriever,
        sparse: Retriever,
        *,
        depth: int = FUSION_DEPTH,
        k: int = RRF_K,
    ) -> None:
        self.dense = dense
        self.sparse = sparse
        self.depth = depth
        self.k = k

    def retrieve(self, query: str, *, top_k: int = 10) -> list[RetrievalResult]:
        fused = reciprocal_rank_fusion(
            self.dense.retrieve(query, top_k=self.depth),
            self.sparse.retrieve(query, top_k=self.depth),
            k=self.k,
        )
        return fused[:top_k]
