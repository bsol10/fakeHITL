"""Chunking within locator boundaries, never across them (BUILD_SPEC.md section 4).

A chunk spanning two locator units (two PDF pages, two table rows) has an
ambiguous anchor, so its citation cannot be checked. A chunk therefore comes
from exactly one parent :class:`~updater.models.Passage`: its text is a slice of
the parent's text and its anchor narrows the parent's. Passages are never
concatenated, and every chunk is also checked with
:func:`updater.provenance.require_derives_within` before it is returned.

Three properties hold for every chunk, and each has a test:

1. ``chunk.text`` is a verbatim slice of ``parent.text``, with no normalisation
   or whitespace collapsing: the extract stage verifies quotes by string match.
2. ``chunk.anchor`` names the parent's locator unit, with a span inside the
   parent's span.
3. Slicing the parent's text by the chunk's anchor offsets returns the chunk's
   text, which is what makes a citation checkable by hand.

A passage that already fits the budget becomes one chunk covering its whole
unit (id ``{parent id}#0``, with a real token count). Nothing is dropped or
merged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from updater.provenance import require_derives_within
from updater.retrieve.tokenize import default_token_counter

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from updater.models import Passage
    from updater.retrieve.tokenize import TokenCounter

__all__ = [
    "DEFAULT_CHUNK_CONFIG",
    "SEPARATORS",
    "ChunkConfig",
    "chunk_passage",
    "chunk_passages",
    "split_spans",
]

SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", "; ", "? ", "! ", ", ", " ")
"""Where text may be cut, cheapest cut first: paragraph, line, sentence, clause,
word. ``"; "`` is the tabular serialisation's field boundary
(``Sheet. Region: North East; Starts 2023: 12340``), so cutting there keeps a
header with its value."""


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """The chunk budget, in word pieces of the embedding model's tokenizer.

    ``max_tokens`` is a hard ceiling; ``min_tokens`` is not a floor. A locator
    unit smaller than it yields a short chunk, because the only text that could
    pad it belongs to another page or row.
    """

    max_tokens: int = 600
    min_tokens: int = 400
    overlap_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")
        if self.min_tokens > self.max_tokens:
            raise ValueError(
                f"min_tokens ({self.min_tokens}) exceeds max_tokens ({self.max_tokens})"
            )
        if not 0.0 <= self.overlap_ratio < 1.0:
            raise ValueError(
                f"overlap_ratio must be in [0, 1), got {self.overlap_ratio}"
            )

    @property
    def overlap_tokens(self) -> int:
        """Tokens of the previous chunk repeated at the start of the next."""
        return int(self.max_tokens * self.overlap_ratio)


DEFAULT_CHUNK_CONFIG = ChunkConfig()
"""400-600 tokens, 15% overlap — the spec's numbers."""


def _split_on(text: str, start: int, end: int, separator: str) -> list[tuple[int, int]]:
    """Cut ``text[start:end]`` at ``separator``, keeping it on the left piece.

    Offsets are absolute into ``text`` throughout this module. Nothing is ever
    re-joined from substrings, so an offset cannot drift from the text it names.
    """
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        found = text.find(separator, cursor, end)
        if found == -1:
            spans.append((cursor, end))
            break
        stop = found + len(separator)
        if stop >= end:
            spans.append((cursor, end))
            break
        spans.append((cursor, stop))
        cursor = stop
    return [span for span in spans if span[1] > span[0]]


def _atoms(
    text: str,
    start: int,
    end: int,
    *,
    count: TokenCounter,
    budget: int,
    separators: Sequence[str],
) -> list[tuple[int, int]]:
    """Break a span into pieces that each fit the budget, cutting as coarsely as it can.

    Falls back to a proportional character split only when no separator is left:
    a 700-token "word" is an encoding failure the ingest quality gate rejects.
    """
    if count.count(text[start:end]) <= budget:
        return [(start, end)]

    for index, separator in enumerate(separators):
        pieces = _split_on(text, start, end, separator)
        if len(pieces) > 1:
            atoms: list[tuple[int, int]] = []
            for piece_start, piece_end in pieces:
                atoms.extend(
                    _atoms(
                        text,
                        piece_start,
                        piece_end,
                        count=count,
                        budget=budget,
                        separators=separators[index + 1 :],
                    )
                )
            return atoms

    tokens = max(1, count.count(text[start:end]))
    chars_per_token = (end - start) / tokens
    step = max(1, int(budget * chars_per_token))
    return [(cursor, min(cursor + step, end)) for cursor in range(start, end, step)]


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Move a span off surrounding whitespace, keeping the offsets truthful."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _atom_budget(config: ChunkConfig) -> int:
    """How finely to break the text before packing it into chunks.

    No coarser than the overlap, not simply ``max_tokens``: an atom larger than
    the overlap can never be carried into the next chunk, so the overlap would
    silently become zero while the chunks still came out the right size.
    """
    if config.overlap_tokens <= 0:
        return config.max_tokens
    return min(config.max_tokens, config.overlap_tokens)


def _overlap_start(
    sizes: Sequence[int], first: int, stop: int, overlap_tokens: int
) -> int:
    """Where the next chunk begins so it repeats ~``overlap_tokens`` of this one.

    Never returns ``first``: the next chunk must start strictly later than the
    one just emitted, or packing loops forever.
    """
    if overlap_tokens <= 0:
        return stop
    start = stop
    carried = 0
    while start > first + 1 and carried + sizes[start - 1] <= overlap_tokens:
        start -= 1
        carried += sizes[start]
    return start


def split_spans(
    text: str,
    *,
    counter: TokenCounter,
    config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
) -> list[tuple[int, int]]:
    """Character spans of ``text``, each within the token budget, with overlap.

    Returned spans are ordered and each is a valid slice of ``text``. Successive
    spans overlap by roughly ``config.overlap_tokens``, so they are a cover, not
    a partition.
    """
    if not text.strip():
        return []
    if counter.count(text) <= config.max_tokens:
        return [(0, len(text))]

    atoms = _atoms(
        text,
        0,
        len(text),
        count=counter,
        budget=_atom_budget(config),
        separators=SEPARATORS,
    )
    sizes = [counter.count(text[start:end]) for start, end in atoms]

    # Packing runs over atom indices rather than character offsets, so the tail
    # can be rebalanced afterwards without re-deriving where the cuts were.
    packed: list[tuple[int, int]] = []
    first = 0
    running = 0
    index = 0
    while index < len(atoms):
        size = sizes[index]
        if index > first and running + size > config.max_tokens:
            packed.append((first, index))
            first = _overlap_start(sizes, first, index, config.overlap_tokens)
            running = sum(sizes[first:index])
            continue
        running += size
        index += 1

    if first < len(atoms):
        packed.append((first, len(atoms)))

    packed = _rebalance_tail(packed, sizes, config)
    spans = [(atoms[start][0], atoms[stop - 1][1]) for start, stop in packed]
    trimmed = [_trim(text, start, end) for start, end in spans]
    return [(start, end) for start, end in trimmed if end > start]


def _rebalance_tail(
    packed: list[tuple[int, int]], sizes: Sequence[int], config: ChunkConfig
) -> list[tuple[int, int]]:
    """Grow a stub final chunk backwards until it reaches ``min_tokens``.

    Greedy packing can leave a few tokens in the last chunk, which embeds badly,
    so the tail of a long unit would be quietly unfindable. Growing backwards
    only adds overlap with the previous chunk, from the same locator unit. The
    tail may not reach the previous chunk's start, or the two would cover one
    span.
    """
    if len(packed) < 2:
        return packed

    start, stop = packed[-1]
    floor = packed[-2][0] + 1
    while start > floor and sum(sizes[start:stop]) < config.min_tokens:
        if sum(sizes[start - 1 : stop]) > config.max_tokens:
            break
        start -= 1

    packed[-1] = (start, stop)
    return packed


def chunk_passage(
    passage: Passage,
    *,
    counter: TokenCounter | None = None,
    config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
) -> list[Passage]:
    """Split one passage into chunks that stay inside its locator unit.

    The parent's locator is copied onto every chunk and only the character span
    narrows, so a chunk of page 7 is anchored to page 7 and nothing else.

    Raises:
        ProvenanceError: a chunk escaped its parent's span. A bug in this
            module, never a data problem.
    """
    counter = counter or default_token_counter()
    parent = passage.anchor
    spans = split_spans(passage.text, counter=counter, config=config)

    chunks: list[Passage] = []
    for ordinal, (start, end) in enumerate(spans):
        anchor = replace(
            parent,
            char_start=parent.char_start + start,
            char_end=parent.char_start + end,
        )
        require_derives_within(parent, anchor)
        text = passage.text[start:end]
        chunks.append(
            replace(
                passage,
                id=f"{passage.id}#{ordinal}",
                text=text,
                anchor=anchor,
                token_count=counter.count(text),
            )
        )
    return chunks


def chunk_passages(
    passages: Iterable[Passage],
    *,
    counter: TokenCounter | None = None,
    config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
) -> list[Passage]:
    """Chunk every passage, in order, loading the tokenizer once."""
    counter = counter or default_token_counter()
    chunks: list[Passage] = []
    for passage in passages:
        chunks.extend(chunk_passage(passage, counter=counter, config=config))
    return chunks
