"""Core data model (BUILD_SPEC.md section 2). Everything downstream depends on it.

Two design rules are encoded in the types rather than left to convention:

*Rule 1, provenance is never lost.* :class:`Passage` and :class:`Evidence`
cannot be constructed without a :class:`SourceAnchor`, so text never becomes a
bare ``str``.

*Rule 2, abstention over confabulation.* Optional fields on :class:`Evidence`
default to ``None``, so returning nothing is the cheapest thing an extractor can
do. A model that must emit a number will invent one.

Provenance records are frozen: an anchor that can be edited after the fact is
not provenance. :class:`Source` is the one mutable type.

Records are keyword-only, so ``char_start`` and ``char_end`` cannot be passed the
wrong way round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypeAlias

__all__ = [
    "BlockLocator",
    "CellLocator",
    "Claim",
    "ComparisonResult",
    "ComparisonStatus",
    "DomLocator",
    "Evidence",
    "Locator",
    "MediaType",
    "PageLocator",
    "Passage",
    "SlideLocator",
    "Source",
    "SourceAnchor",
    "TimeLocator",
    "UpdateFrequency",
]


class MediaType(StrEnum):
    """Canonical IANA media types for the formats in BUILD_SPEC.md section 3.

    Fields elsewhere are typed ``str``, not ``MediaType``: members of a
    ``StrEnum`` are strings, so these work anywhere a media type is expected,
    and a sniffed type not listed here still round trips. Sniffing is
    authoritative, not this list.
    """

    PDF = "application/pdf"
    HTML = "text/html"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    CSV = "text/csv"
    TXT = "text/plain"
    MARKDOWN = "text/markdown"
    PNG = "image/png"
    JPEG = "image/jpeg"


class UpdateFrequency(StrEnum):
    """How often a source is expected to be revised.

    ``UNKNOWN`` is the honest default when the publisher states no cadence.
    There is deliberately no "biannual": it reads as both twice a year and every
    two years. Each of the dashboard's four labels (Annual, Biennial,
    Half-yearly, Quarterly) maps to one member by meaning.
    """

    ANNUAL = "annual"
    BIENNIAL = "biennial"
    """Every two years."""
    HALF_YEARLY = "half-yearly"
    """Twice a year."""
    QUARTERLY = "quarterly"
    MONTHLY = "monthly"
    IRREGULAR = "irregular"
    UNKNOWN = "unknown"


class ComparisonStatus(StrEnum):
    """Outcome of comparing a prior-year claim against this year's evidence.

    ``CANNOT_VERIFY`` is the default, not the failure case. It is what the
    system emits when retrieval came back thin, and it is strictly preferable to
    a confidently wrong ``UNCHANGED``.
    """

    UNCHANGED = "unchanged"
    UPDATED = "updated"
    NEW = "new"
    CANNOT_VERIFY = "cannot-verify"


# Locators: the format-specific half of a citation (BUILD_SPEC.md section 3).
# Each variant carries a literal ``kind`` tag so it survives a round trip through
# YAML, JSON or SQLite, where isinstance is not available.


@dataclass(frozen=True, slots=True)
class PageLocator:
    """A page of a paginated document. PDF, and images treated as one page."""

    page: int
    kind: Literal["page"] = "page"


@dataclass(frozen=True, slots=True)
class BlockLocator:
    """A block-level element. DOCX, Markdown and plain text.

    ``style`` is the source's own name for the block ("Heading 1", "Normal",
    "blockquote"), or ``""`` for formats that have no style concept.
    """

    paragraph_index: int
    style: str
    kind: Literal["block"] = "block"


@dataclass(frozen=True, slots=True)
class CellLocator:
    """A rectangular range within one grid. XLSX, CSV and DOCX tables.

    ``cell_range`` is A1 notation ("B4:B12"). ``sheet`` names the grid: the
    worksheet, the CSV's file stem, or a DOCX table's ``Table 3 (para 194)``. It
    is read by people, never parsed; if it ever needs parsing, split the variant
    (see DESIGN.md decisions log).
    """

    sheet: str
    cell_range: str
    kind: Literal["cell"] = "cell"


@dataclass(frozen=True, slots=True)
class SlideLocator:
    """One shape on one slide. PPTX. Slides are 1-indexed as the deck shows."""

    slide: int
    shape_index: int
    kind: Literal["slide"] = "slide"


@dataclass(frozen=True, slots=True)
class DomLocator:
    """A node in an HTML document.

    ``heading_trail`` is the chain of enclosing headings, outermost first. It
    keeps a web citation legible once the page is restructured and the CSS path
    no longer resolves. A tuple, not a list, so anchors stay hashable.
    """

    css_path: str
    heading_trail: tuple[str, ...] = ()
    kind: Literal["dom"] = "dom"


@dataclass(frozen=True, slots=True)
class TimeLocator:
    """A span of an audio or video transcript, in seconds from the start."""

    start_sec: float
    end_sec: float
    kind: Literal["time"] = "time"


Locator: TypeAlias = (
    PageLocator | BlockLocator | CellLocator | SlideLocator | DomLocator | TimeLocator
)
"""Every way this system can point at a location inside a source."""


@dataclass(slots=True, kw_only=True)
class Source:
    """A registry entry: one approved source (design rule 3).

    Mutable, unlike every other record here: the fetch stage updates
    ``last_checked`` and ``last_content_hash`` in place.
    """

    id: str
    title: str
    url_or_path: str
    owner: str
    topic: str
    year: int
    update_frequency: UpdateFrequency
    media_type: str
    """IANA media type. See :class:`MediaType` for the canonical values."""

    last_checked: datetime | None = None
    last_content_hash: str | None = None
    """SHA-256 of the *normalised extracted text*, never of the raw bytes — a
    PDF re-exported with a new timestamp has different bytes and identical
    content (BUILD_SPEC.md section 4, fetch)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAnchor:
    """The citation primitive: which source, and where inside it.

    ``char_start`` and ``char_end`` are offsets into the extracted text of the
    unit ``locator`` names (the page, the paragraph, the cell range), not into
    the whole document. That lets the extract stage verify a quoted span by
    plain string match, and keeps the anchor valid when unrelated parts of the
    document shift.
    """

    source_id: str
    locator: Locator
    char_start: int
    char_end: int
    extracted_at: datetime

    def __post_init__(self) -> None:
        if self.char_start < 0:
            raise ValueError(f"char_start must be >= 0, got {self.char_start}")
        if self.char_end < self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) precedes char_start ({self.char_start})"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Passage:
    """A span of extracted text that knows where it came from.

    The unit that moves through the pipeline. Chunking splits passages within
    locator boundaries, never across them (BUILD_SPEC.md section 4).
    """

    id: str
    source_id: str
    text: str
    anchor: SourceAnchor
    token_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence:
    """One structured finding, extracted from retrieved passages.

    Every field except ``finding``, ``anchor`` and ``confidence`` defaults to
    ``None`` (design rule 2): an extractor that found no statistic omits the
    argument, which is less work than inventing one.

    There is no ``quote`` field. The supporting span is
    ``passage.text[anchor.char_start:anchor.char_end]``, derived so it cannot
    drift out of sync with the anchor that justifies it.
    """

    finding: str
    statistic: str | None = None
    method: str | None = None
    as_of_date: date | None = None
    caveat: str | None = None
    anchor: SourceAnchor
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Claim:
    """A unit of the prior-year report, and where it was said.

    ``prior_anchor`` points into last year's report, not this year's sources,
    so a reviewer can read the old sentence next to the new evidence.
    """

    id: str
    text: str
    prior_anchor: SourceAnchor


@dataclass(frozen=True, slots=True, kw_only=True)
class ComparisonResult:
    """What this year's sources say about one prior-year claim.

    ``rationale`` is required for every status, including ``UNCHANGED``. A
    classification a reviewer cannot audit is not worth having.
    """

    claim_id: str
    status: ComparisonStatus
    current_evidence: list[Evidence] = field(default_factory=list)
    rationale: str
