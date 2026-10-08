"""Embedding chunks locally, with no silently truncated text.

BUILD_SPEC.md section 4 names ``all-MiniLM-L6-v2`` and a chunk size of 400-600
tokens. Those two numbers do not fit together, and the way they fail is silent:
**the model's input window is 256 word pieces**, and sentence-transformers
truncates anything longer without a warning. A 600-token chunk would be
retrievable only by its first 40%, and the other 60% would be missing from the
index while looking present in the database — an absence of evidence that the
compare stage would later read as a finding about the world.

So the chunk stays the size the spec asks for, and this module makes it fit:

*A chunk is embedded as one or more overlapping windows of at most 254 word
pieces, and every window gets its own vector, all of them pointing back at the
same chunk.*

Retrieval scores a chunk by its **best** window (see
:mod:`updater.retrieve.search`), which is better than the obvious alternative of
averaging the windows into one vector. This corpus is statistics: a single row
stating an apprenticeship figure is the thing a query is looking for, and
averaging it with 500 tokens of surrounding prose is how it stops being findable.

Windows are produced by the chunker's own splitter with a smaller budget, so a
window never crosses a locator boundary either — it is a slice of a slice.
Windows carry offsets but no anchors of their own: a window is an artefact of
this model's input size, not a place in a document, and minting anchors for one
would put a citation in the draft that points at an arbitrary 254-token
fragment. The chunk is the unit of provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from updater.certs import ensure_system_certs
from updater.retrieve.chunk import ChunkConfig, split_spans
from updater.retrieve.tokenize import (
    EMBEDDING_MODEL_ID,
    USABLE_WINDOW_TOKENS,
    default_token_counter,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from updater.models import Passage
    from updater.retrieve.tokenize import TokenCounter

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "USABLE_WINDOW_TOKENS",
    "WINDOW_CONFIG",
    "EmbeddedWindow",
    "Embedder",
    "EmbeddingUnavailable",
    "SentenceTransformerEmbedder",
    "embed_chunks",
    "window_spans",
]

EMBEDDING_DIMENSIONS = 384
"""Output width of all-MiniLM-L6-v2. Pinned because the vec0 table declares it."""

WINDOW_CONFIG = ChunkConfig(
    max_tokens=USABLE_WINDOW_TOKENS,
    min_tokens=USABLE_WINDOW_TOKENS // 2,
    overlap_ratio=0.15,
)
"""Window budget: what actually fits through the model in one forward pass.

The overlap matters more here than it does between chunks. A sentence split
across two windows is embedded twice, each time with half its context; the
overlap gives it at least one window where it appears whole.
"""


class EmbeddingUnavailable(RuntimeError):
    """The embedding model could not be loaded."""


class Embedder(Protocol):
    """Turns text into unit-length vectors.

    A protocol so tests can inject a deterministic stub — loading a real model
    to assert that a store round-trips a vector is 20 seconds of nothing.
    """

    dimensions: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbeddedWindow:
    """One vector, and the slice of one chunk it was computed from.

    ``char_start`` and ``char_end`` index into the *chunk's* text. They exist for
    debugging — to answer "which part of this chunk actually matched?" — and are
    deliberately not anchors; see the module docstring.
    """

    chunk_id: str
    ordinal: int
    char_start: int
    char_end: int
    vector: list[float]


class SentenceTransformerEmbedder:
    """all-MiniLM-L6-v2, loaded locally. No API calls, no vector service.

    The model is loaded on first use rather than in ``__init__`` so that
    constructing one is free: the CLI builds an embedder before it knows whether
    the command will need it, and paying 20 seconds of torch import to print a
    usage error would be poor manners.
    """

    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, model_id: str = EMBEDDING_MODEL_ID) -> None:
        self.model_id = model_id
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        ensure_system_certs()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:  # pragma: no cover - depends on the install
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed. Install the extra: "
                "uv sync --extra retrieve"
            ) from error
        try:
            model = SentenceTransformer(self.model_id)
        except Exception as error:  # pragma: no cover - network or cache state
            raise EmbeddingUnavailable(
                f"could not load {self.model_id}: {error}. The first run "
                f"downloads it from the Hugging Face hub; behind a "
                f"TLS-intercepting proxy see updater.certs."
            ) from error

        # Belt and braces against the truncation this module exists to avoid:
        # if a future release ships a different window, windowing must follow it
        # rather than keep using a stale constant.
        window = getattr(model, "max_seq_length", None)
        if isinstance(window, int) and window < USABLE_WINDOW_TOKENS:
            raise EmbeddingUnavailable(
                f"{self.model_id} accepts {window} tokens but windows are built "
                f"for {USABLE_WINDOW_TOKENS}; text would be truncated silently. "
                f"Update USABLE_WINDOW_TOKENS in updater.retrieve.tokenize."
            )
        self._model = model
        return model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Unit-length vectors, one per input, in order.

        Normalised at encode time so the store only ever holds unit vectors and
        cosine distance is the only distance anything has to reason about.
        """
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(  # type: ignore[attr-defined]
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]


def window_spans(text: str, *, counter: TokenCounter) -> list[tuple[int, int]]:
    """Character spans of ``text``, each short enough to embed whole."""
    return split_spans(text, counter=counter, config=WINDOW_CONFIG)


def embed_chunks(
    chunks: Iterable[Passage],
    *,
    embedder: Embedder,
    counter: TokenCounter | None = None,
    batch_size: int = 64,
) -> list[EmbeddedWindow]:
    """Embed every chunk, windowing any that exceed the model's input size.

    Returns one :class:`EmbeddedWindow` per window, in chunk order. A chunk that
    fits the window yields exactly one, whose span covers the whole chunk.
    """
    counter = counter or default_token_counter()

    slots: list[tuple[str, int, int, int]] = []
    texts: list[str] = []
    for chunk in chunks:
        spans = window_spans(chunk.text, counter=counter)
        for ordinal, (start, end) in enumerate(spans):
            slots.append((chunk.id, ordinal, start, end))
            texts.append(chunk.text[start:end])

    vectors: list[list[float]] = []
    for offset in range(0, len(texts), batch_size):
        vectors.extend(embedder.encode(texts[offset : offset + batch_size]))

    if len(vectors) != len(slots):
        raise EmbeddingUnavailable(
            f"embedder returned {len(vectors)} vectors for {len(slots)} windows; "
            f"they are matched up by position, so an index built from this "
            f"would attach vectors to the wrong text"
        )
    for vector in vectors[:1]:
        if len(vector) != embedder.dimensions:
            raise EmbeddingUnavailable(
                f"embedder produced {len(vector)}-dimensional vectors but "
                f"declares {embedder.dimensions}"
            )

    return [
        EmbeddedWindow(
            chunk_id=chunk_id,
            ordinal=ordinal,
            char_start=start,
            char_end=end,
            vector=vector,
        )
        for (chunk_id, ordinal, start, end), vector in zip(slots, vectors, strict=True)
    ]
