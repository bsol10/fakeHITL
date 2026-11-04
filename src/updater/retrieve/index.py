"""Building the index: extract, chunk, embed and store each registered source.

Every function takes a :class:`~updater.models.Source` obtained from
:mod:`updater.registry`; nothing here constructs one (design rule 3).

One bad source does not stop the run, but it is reported as a **failure**,
never as a source that produced nothing: the two look identical downstream and
mean opposite things.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from updater.ingest import IngestError, extract
from updater.retrieve.chunk import DEFAULT_CHUNK_CONFIG, ChunkConfig, chunk_passages
from updater.retrieve.embed import embed_chunks
from updater.retrieve.tokenize import default_token_counter

if TYPE_CHECKING:
    from updater.models import Source
    from updater.registry import Registry
    from updater.retrieve.embed import Embedder
    from updater.retrieve.store import PassageStore
    from updater.retrieve.tokenize import TokenCounter

__all__ = ["IndexFailure", "IndexReport", "index_registry", "index_source"]


@dataclass(frozen=True, slots=True)
class IndexReport:
    """What indexing one source produced."""

    source_id: str
    path: Path
    passages: int
    chunks: int
    windows: int

    def __str__(self) -> str:
        return (
            f"{self.source_id}: {self.passages} passage(s) -> {self.chunks} chunk(s), "
            f"{self.windows} vector(s)"
        )


@dataclass(frozen=True, slots=True)
class IndexFailure:
    """A source that could not be indexed, and why. Returned rather than raised,
    so a batch run reports its failures beside its successes."""

    source_id: str
    path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.source_id}: {self.reason}"


def index_source(
    source: Source,
    path: Path,
    *,
    store: PassageStore,
    embedder: Embedder,
    counter: TokenCounter | None = None,
    config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
) -> IndexReport:
    """Extract, chunk, embed and store one approved source.

    Raises:
        IngestError: extraction failed. The index is left unchanged.
    """
    counter = counter or default_token_counter()
    passages = extract(path, source)
    chunks = chunk_passages(passages, counter=counter, config=config)
    windows = embed_chunks(chunks, embedder=embedder, counter=counter)
    store.replace_source(source, chunks, windows)
    return IndexReport(
        source_id=source.id,
        path=path,
        passages=len(passages),
        chunks=len(chunks),
        windows=len(windows),
    )


def index_registry(
    registry: Registry,
    *,
    store: PassageStore,
    embedder: Embedder,
    counter: TokenCounter | None = None,
    config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
    only: set[str] | None = None,
) -> tuple[list[IndexReport], list[IndexFailure]]:
    """Index every local source in the registry, or the subset named by ``only``."""
    counter = counter or default_token_counter()
    reports: list[IndexReport] = []
    failures: list[IndexFailure] = []

    for source, path in registry.local_paths():
        if only is not None and source.id not in only:
            continue
        if not path.is_file():
            failures.append(
                IndexFailure(
                    source_id=source.id,
                    path=path,
                    reason=(
                        f"registered at {source.url_or_path} but there is no file there"
                    ),
                )
            )
            continue
        try:
            reports.append(
                index_source(
                    source,
                    path,
                    store=store,
                    embedder=embedder,
                    counter=counter,
                    config=config,
                )
            )
        except IngestError as error:
            failures.append(
                IndexFailure(source_id=source.id, path=path, reason=str(error))
            )

    return reports, failures
