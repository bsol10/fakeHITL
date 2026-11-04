"""The adapter contract and the dispatch registry (BUILD_SPEC.md section 3).

Adding a format means adding one adapter and, if needed, one locator variant.
The registry, not the adapter, owns sniffing and the quality gate, so a new
adapter cannot skip either.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Protocol

from updater.ingest.errors import UnsupportedMediaType
from updater.ingest.quality import apply_quality_gate
from updater.ingest.sniff import sniff_media_type
from updater.models import Locator, Passage, Source, SourceAnchor

__all__ = [
    "CHARS_PER_TOKEN",
    "Extractor",
    "ExtractorRegistry",
    "build_passage",
    "estimate_tokens",
]

LOGGER = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
"""Crude characters-per-token ratio; see :func:`estimate_tokens`."""


class Extractor(Protocol):
    """One extractor per media type, every one returning ``list[Passage]``.

    ``media_types`` is a ``ClassVar`` where BUILD_SPEC.md writes ``set[str]``,
    because it is class-level state. The contract is unchanged.
    """

    media_types: ClassVar[set[str]]

    def extract(self, path: Path, source: Source) -> list[Passage]: ...


def estimate_tokens(text: str) -> int:
    """Rough token count at ~4 characters per token.

    Deliberately crude, so nobody mistakes it for real counting. Chunking
    replaces it with a count in the embedding model's word pieces; keep the
    tokenizer dependency out of ingest.
    """
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def build_passage(
    *,
    source: Source,
    locator: Locator,
    text: str,
    locator_key: str,
    extracted_at: datetime,
) -> Passage:
    """Mint an anchored passage covering one whole locator unit.

    The anchor spans the entire text: at ingest the passage *is* the unit the
    locator names. Chunking narrows it, never crossing these boundaries.
    """
    anchor = SourceAnchor(
        source_id=source.id,
        locator=locator,
        char_start=0,
        char_end=len(text),
        extracted_at=extracted_at,
    )
    return Passage(
        id=f"{source.id}:{locator_key}",
        source_id=source.id,
        text=text,
        anchor=anchor,
        token_count=estimate_tokens(text),
    )


class ExtractorRegistry:
    """Media type to extractor, dispatching on sniffed content.

    Owns the quality gate so that adding an adapter cannot mean forgetting it.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {}

    def register(self, extractor: Extractor) -> None:
        """Claim every media type the extractor declares.

        Refuses to shadow an existing claim: two adapters silently competing for
        one media type would change citation quality with no visible cause.
        """
        for media_type in sorted(extractor.media_types):
            existing = self._extractors.get(media_type)
            if existing is not None and existing is not extractor:
                raise ValueError(
                    f"{media_type!r} is already handled by "
                    f"{type(existing).__name__}; refusing to shadow it with "
                    f"{type(extractor).__name__}"
                )
            self._extractors[media_type] = extractor

    @property
    def media_types(self) -> frozenset[str]:
        """Every media type this registry can currently handle."""
        return frozenset(self._extractors)

    def extractor_for(self, media_type: str, *, path: Path) -> Extractor:
        try:
            return self._extractors[media_type]
        except KeyError:
            raise UnsupportedMediaType(
                path=path, media_type=media_type, registered=self._extractors
            ) from None

    def extract(self, path: Path, source: Source) -> list[Passage]:
        """Sniff, dispatch, extract, and gate — the whole ingest step."""
        media_type = sniff_media_type(path)
        if media_type != source.media_type:
            LOGGER.warning(
                "source %s is registered as %s but %s sniffs as %s; "
                "trusting the sniffed type",
                source.id,
                source.media_type,
                path,
                media_type,
            )
        extractor = self.extractor_for(media_type, path=path)
        passages = extractor.extract(path, source)
        return apply_quality_gate(passages, source=source, path=path)
