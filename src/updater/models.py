"""Core data model for the ASIL annual research update agent.

Defined before any pipeline code, per BUILD_SPEC.md section 2. Everything
downstream depends on these types.

Two of the five non-negotiable design rules are encoded in the types themselves
rather than left to convention:

*Rule 1 — provenance is never lost.* :class:`Passage` and :class:`Evidence`
cannot be constructed without a :class:`SourceAnchor`. There is no stage of the
pipeline at which text is allowed to become a bare ``str``.

*Rule 2 — abstention over confabulation.* Every optional field on
:class:`Evidence` defaults to ``None``, so returning nothing is the cheapest
thing an extractor can do. A model that must emit a number will invent one;
making null the easy path is what stops that.

Provenance records are frozen — an anchor that could be edited after the fact is
not provenance. :class:`Source` is the one mutable type, because the fetch stage
writes ``last_checked`` and ``last_content_hash`` back to the registry entry.

Records use keyword-only fields. That costs a few characters at each call site
and buys immunity from the silent failure where ``char_start`` and ``char_end``
are passed the wrong way round.
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


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class MediaType(StrEnum):
    """Canonical IANA media types for the formats in BUILD_SPEC.md section 3.

    Fields are typed ``str`` rather than ``MediaType`` on purpose: members of a
    ``StrEnum`` *are* strings, so these constants can be used anywhere a media
    type is expected, while a sniffed type that is not listed here still round
    trips instead of raising. Sniffing is authoritative, not this list.
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

    Drives which sources are worth re-fetching in a given run. ``UNKNOWN`` is
    the honest default for a source whose publisher has no stated cadence.

    There is no "biannual". It means twice a year to some readers and every two
    years to others, and the dashboard prototype uses both cadences, as
    "Half-yearly" and "Biennial". Each of the prototype's four labels names one
    member here by meaning; monthly, irregular and unknown have no label there.
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


# --------------------------------------------------------------------------- #
# Locators — the format-specific half of a citation (BUILD_SPEC.md section 3)
# --------------------------------------------------------------------------- #
#
# A discriminated union: each variant carries a literal ``kind`` tag so it
# survives a round trip through YAML, JSON or SQLite, where isinstance checks
# are not available. Adding a format means adding one variant here and one
# adapter in ``updater.ingest`` — nothing else in the pipeline changes.


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
    """A rectangular range within one sheet. XLSX and CSV.

    ``cell_range`` is A1 notation ("B4:B12"). CSV has a single implicit sheet;
    use the file stem as ``sheet`` so the rendered citation still reads.
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

    ``heading_trail`` is the chain of enclosing headings, outermost first, which
    is what makes a web citation legible to a reviewer once the page has been
    restructured and the CSS path no longer resolves. It is a tuple rather than
    a list so anchors stay hashable.
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


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(slots=True, kw_only=True)
class Source:
    """A registry entry: one approved source, and what we know about it.

    Design rule 3 — if it isn't in the registry, it isn't evidence.

    Mutable, unlike everything else in this module: the fetch stage updates
    ``last_checked`` and ``last_content_hash`` in place after each run.
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
    unit named by ``locator`` — the page, the paragraph, the cell range. Keeping
    them locator-relative rather than document-relative is what lets the
    extract stage verify a quoted span by plain string match, and what keeps the
    anchor valid when unrelated parts of the document shift.

    Frozen. Once minted, an anchor travels unchanged through chunking,
    retrieval, extraction and drafting.
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
    locator boundaries and never across them, so a chunk cannot straddle two
    PDF pages and leave its anchor ambiguous (BUILD_SPEC.md section 4).
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
    ``None``. That is design rule 2 expressed in the constructor: an extractor
    that found no statistic omits the argument, and omitting it is less work
    than inventing one.

    There is no ``quote`` field. The supporting span is
    ``passage.text[anchor.char_start:anchor.char_end]`` — deriving it rather
    than storing it means the quote cannot drift out of sync with the anchor
    that is supposed to justify it.
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

    ``prior_anchor`` points into last year's report, not into this year's
    sources. It is what makes the comparison auditable: a reviewer can read the
    old sentence next to the new evidence.
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
