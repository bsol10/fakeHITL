"""Retrieval over the chunk index: dense, sparse, and the two fused.

BUILD_SPEC.md section 4 wants hybrid — *"BM25 (rank_bm25) plus dense, merged.
Statistics and proper nouns are exactly what dense retrieval misses"* — and
that is right for this corpus in a way worth spelling out, because it is the
reason the interface below has the shape it does.

A serialised table row reads ``Apprenticeship starts. Region: North East;
Starts 2023: 12340``. Ask an embedding model for "apprenticeship starts in the
North East" and it does well. Ask it for "12340" and it does nothing useful:
that number is three word pieces with no semantics, and the nearest neighbours
of its vector are other numbers. Measured on ASIL's own index, the row reading
``Avg Salary Low (AUD): $109,860`` is dense rank 52 for the query ``109,860``.
A reviewer checking last year's figure searches for exactly that, and full-text
search finds it on the first try.

So retrieval is a **protocol**, not a class hierarchy: :class:`Retriever` is
anything that turns a query into ranked chunks, and merging two of them is a
function over their results rather than a change to either.

*Fusion is reciprocal rank fusion, over ranks, never scores.* Cosine similarity
and BM25 are on scales that have nothing in common, and normalising one to look
like the other is a guess about a relationship that does not exist. RRF gives a
chunk ``1 / (k + rank)`` from each list it appears in, with ``k = 60`` and each
retriever read to its top 50 (BACKEND_PLAN.md phase 2).

*A tie goes to the better lexical rank.* Ties are not a corner case: a chunk only
dense found at rank 1 and a chunk only full-text search found at rank 1 both
score exactly 1/61, and that is the ``109,860`` query on the real index. RRF has
no opinion between them, so something else must. A lexical hit is checkable —
the query's words are in the text — while a dense rank is a similarity estimate,
and for a number that estimate is noise. The rule only orders results RRF scores
equally; it never overrides a difference.

*The fused score is not a relevance measure.* It is a function of two ranks, so
whatever comes first scores the same whether it is an exact match or the best of
a bad lot. BUILD_SPEC.md's compare stage defaults to ``cannot-verify`` when
retrieval "came back thin", and thinness has to be read from the inputs, not
from this number.
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

    ``retriever`` is carried on the result rather than inferred by the caller
    because the moment there are two of them, "why did this come back?" becomes
    the question a reviewer asks, and a merged list that cannot answer it is a
    list nobody can debug. The two ranks are the rest of that answer.
    """

    passage: Passage
    score: float
    retriever: str
    window_ordinal: int = 0
    """Which embedding window matched best. Meaningless without a dense rank,
    which is why it defaults rather than being required."""
    dense_rank: int | None = None
    """1-based rank in the dense results; None when dense did not return it."""
    sparse_rank: int | None = None
    """1-based rank in the full-text results; None when they did not return it."""

    @property
    def anchor_summary(self) -> str:
        """A terse rendering of where this came from, for logs and the CLI.

        Not the citation renderer — BUILD_SPEC.md's draft stage owns that, with
        the source title in front of it and design rule 4 to satisfy.
        """
        locator = self.passage.anchor.locator
        return f"{self.passage.source_id} {locator}"


class Retriever(Protocol):
    """Turns a query into ranked chunks. Dense, sparse, or a fusion of both."""

    name: str

    def retrieve(self, query: str, *, top_k: int = 10) -> list[RetrievalResult]: ...


class DenseRetriever:
    """Nearest neighbours in embedding space, best window per chunk.

    Scores are cosine similarities, so they are comparable across queries — which
    matters more here than in most systems, because BUILD_SPEC.md's compare stage
    defaults to ``cannot-verify`` when retrieval "came back thin" and something
    has to define thin. That threshold is the compare stage's to set, but it can
    only exist because the number below means the same thing every time.
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

    The retriever that can find a number. Every word of the query is searched
    for as a word, never parsed as FTS5 syntax, so a pasted sentence with "not"
    or a colon in it cannot raise. Scores are BM25, higher better, and mean
    nothing outside the query that produced them.
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
