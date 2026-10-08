"""Chunking, embedding and retrieval (BUILD_SPEC.md section 4).

Chunks run ~400-600 tokens with ~15% overlap, and **never cross a locator
boundary**. A chunk that spans two PDF pages has an ambiguous anchor, and an
ambiguous anchor is an uncheckable citation. This is the rule that keeps
citations honest, and :mod:`updater.retrieve.chunk` calls
:func:`updater.provenance.require_derives_within` on every chunk it emits.

Tokens are counted in the embedding model's own word pieces, because the only
budget that can actually be violated is that model's input window.

Embeddings are local (``sentence-transformers``, ``all-MiniLM-L6-v2``) and live
in SQLite with ``sqlite-vec``. No separate vector database service. The model's
window is 256 word pieces — smaller than a chunk — so a chunk is embedded as
overlapping windows and scored by its best one, rather than being truncated to
its first 40% without anybody being told.

Retrieval is hybrid: full-text BM25 (SQLite FTS5, in the same file) and dense,
fused by reciprocal rank. Statistics and proper nouns — exactly what this
project is made of — are what dense retrieval misses.
:class:`~updater.retrieve.search.Retriever` is the seam.

Usage::

    from updater.registry import load_registry
    from updater.retrieve import PassageStore, SentenceTransformerEmbedder
    from updater.retrieve.index import index_registry

    with PassageStore() as store:
        index_registry(load_registry(), store=store,
                       embedder=SentenceTransformerEmbedder())
"""

from __future__ import annotations

from updater.retrieve.chunk import (
    DEFAULT_CHUNK_CONFIG,
    ChunkConfig,
    chunk_passage,
    chunk_passages,
    split_spans,
)
from updater.retrieve.embed import (
    EMBEDDING_DIMENSIONS,
    EmbeddedWindow,
    Embedder,
    EmbeddingUnavailable,
    SentenceTransformerEmbedder,
    embed_chunks,
)
from updater.retrieve.index import (
    IndexFailure,
    IndexReport,
    index_registry,
    index_source,
)
from updater.retrieve.search import (
    DenseRetriever,
    HybridRetriever,
    RetrievalResult,
    Retriever,
    SparseRetriever,
    reciprocal_rank_fusion,
)
from updater.retrieve.store import (
    IncompatibleIndex,
    PassageStore,
    ScoredChunk,
    StoreError,
)
from updater.retrieve.tokenize import (
    EMBEDDING_MODEL_ID,
    MODEL_WINDOW_TOKENS,
    USABLE_WINDOW_TOKENS,
    TokenCounter,
    TokenizerUnavailable,
    WordPieceCounter,
    default_token_counter,
)

__all__ = [
    "DEFAULT_CHUNK_CONFIG",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL_ID",
    "MODEL_WINDOW_TOKENS",
    "USABLE_WINDOW_TOKENS",
    "ChunkConfig",
    "DenseRetriever",
    "EmbeddedWindow",
    "Embedder",
    "EmbeddingUnavailable",
    "HybridRetriever",
    "IncompatibleIndex",
    "IndexFailure",
    "IndexReport",
    "PassageStore",
    "RetrievalResult",
    "Retriever",
    "ScoredChunk",
    "SentenceTransformerEmbedder",
    "SparseRetriever",
    "StoreError",
    "TokenCounter",
    "TokenizerUnavailable",
    "WordPieceCounter",
    "chunk_passage",
    "chunk_passages",
    "default_token_counter",
    "embed_chunks",
    "index_registry",
    "index_source",
    "reciprocal_rank_fusion",
    "split_spans",
]
