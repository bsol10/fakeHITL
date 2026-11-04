"""Chunking, embedding and retrieval (BUILD_SPEC.md section 4).

- ``chunk``: ~400-600-token chunks with ~15% overlap that **never cross a
  locator boundary**, since an ambiguous anchor is an uncheckable citation.
  Tokens are word pieces of the embedding model's tokenizer (``tokenize``).
- ``embed``: local ``all-MiniLM-L6-v2``. Its window is smaller than a chunk, so
  a chunk is embedded as overlapping windows, never truncated.
- ``store``: chunks, anchors, vectors (``sqlite-vec``) and a full-text index
  (FTS5) in one SQLite file.
- ``search``: dense, full-text BM25, or both fused by reciprocal rank;
  :class:`~updater.retrieve.search.Retriever` is the seam.
- ``index``: extract, chunk, embed and store each registered source.

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
