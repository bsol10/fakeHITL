"""Chunking, within locator boundaries and never across them.

BUILD_SPEC.md section 4: *"~400-600 tokens with ~15% overlap. Chunk within
locator boundaries, never across them. A chunk must not span two PDF pages, or
its anchor is ambiguous. This is the rule that keeps citations honest."*

The design follows from that sentence. A chunk is produced from exactly one
parent :class:`~updater.models.Passage`, its text is a *slice* of the parent's
text, and its anchor narrows the parent's anchor rather than being computed
afresh. Two passages are never concatenated, at any point, for any reason —
which is what makes the rule true by construction rather than by vigilance.

Every chunk is then checked against
:func:`updater.provenance.require_derives_within` before it is returned. That is
belt and braces on purpose: the construction argument above is only as good as
the code implementing it, and this is the failure that produces a draft full of
citations that point at the wrong page while reading perfectly well.

Three properties hold for every chunk this module emits, and each has a test:

1. ``chunk.text`` is a verbatim slice of ``parent.text`` — no normalisation, no
   re-joining, no whitespace collapsing. The extract stage verifies quoted spans
   by plain string match, and that check is worthless if chunking rewrites text.
2. ``chunk.anchor`` names the same locator unit as its parent, with a span
   inside the parent's span.
3. Slicing the parent's text by the chunk's anchor offsets returns the chunk's
   text. This is the property that makes a citation checkable by hand.

A passage that already fits the budget is emitted as a single chunk covering its
whole unit rather than passed through unchanged: downstream everything is a
chunk, chunk ids are derived (``{parent id}#0``), and the token count becomes a
real one. Nothing is dropped and nothing is merged.
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
"""Where text may be cut, in descending order of how little the cut costs.

Paragraph, then line, then sentence, then clause, then word. ``"; "`` earns its
place from the tabular serialisation — a row reads
``Sheet. Region: North East; Starts 2023: 12340; ...``, so the semicolon is that
format's sentence boundary and cutting there keeps a header with its value.
"""


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """The chunk budget, in word pieces of the embedding model's tokenizer.

    ``max_tokens`` is a hard ceiling; ``min_tokens`` is an aspiration, because a
    locator unit smaller than it cannot be padded — the only text that could pad
    it belongs to a different page or a different row, and borrowing it is the
    one thing this module may not do. A short unit yields a short chunk.
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

    Falls back to a proportional character split only when no separator is left
    — a single 700-token "word" is not real prose, and the quality gate has
    already rejected the encoding failures that produce one.
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

    Not simply ``max_tokens``. Atoms are the units both the chunk budget *and*
    the overlap are expressed in, so an atom larger than the overlap can never
    be carried into the next chunk — and the overlap silently becomes zero.

    That was a real defect, and it failed in the quiet direction: chunks came out
    the right size, tiled the text correctly, and had no overlap at all, so a
    sentence sitting on a boundary appeared whole in neither chunk. The tests
    measure the gap between consecutive chunks for exactly this reason.

    Cutting no coarser than the overlap keeps every cut on a real boundary that
    the separator list found — sentences, at the default settings — while
    guaranteeing the overlap can actually be filled.
    """
    if config.overlap_tokens <= 0:
        return config.max_tokens
    return min(config.max_tokens, config.overlap_tokens)


def _overlap_start(
    sizes: Sequence[int], first: int, stop: int, overlap_tokens: int
) -> int:
    """Where the next chunk begins so it repeats ~``overlap_tokens`` of this one.

    Never returns ``first``: the next chunk must start strictly later than the
    one just emitted, or packing makes no progress and loops forever. That is
    the whole reason this is a named function with a test of its own.
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

    Greedy packing leaves whatever is left over in the last chunk, which can be
    a handful of tokens. A stub is not wrong — its anchor is still exact — but it
    embeds badly: a 20-token fragment retrieves on almost nothing, so the tail of
    every long page becomes quietly unfindable, which reads downstream as an
    absence of evidence.

    Extending backwards only ever adds *more overlap* with the previous chunk,
    which is text from the same locator unit, so provenance is unaffected. The
    guard is that the tail may not swallow the previous chunk's start: two chunks
    covering the same span are one chunk and a duplicate.
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
    narrows, so a chunk of page 7 is anchored to page 7 and to nothing else.
    Each chunk is checked with
    :func:`~updater.provenance.require_derives_within` before it is returned.

    Raises:
        ProvenanceError: a chunk escaped its parent's span. That is a bug in
            this module, never a data problem — it means a citation would have
            been wrong.
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
