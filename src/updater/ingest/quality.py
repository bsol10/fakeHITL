"""The extraction quality gate (BUILD_SPEC.md section 3).

Three checks, run over every adapter's output before the passages are accepted:

* non-empty text,
* alphanumeric characters at 60% or more of the total (catches OCR garbage),
* no single whitespace-delimited run over 50 characters (catches the ligature
  and encoding failures that turn a paragraph into one unbroken smear), with
  URLs and email addresses exempt because they are long by nature.

A failure fails the *whole* extraction rather than dropping the offending
passage. Dropping it silently is precisely the behaviour BUILD_SPEC.md section 3
rules out: the missing text would reappear downstream as an absence of evidence,
which the compare stage reads as a real finding about the world.

One deliberate refinement on the spec. The ratio check is skipped for passages
under :data:`RATIO_CHECK_MIN_CHARS` characters, because on a short fragment the
ratio is dominated by a single space or bracket — "p. 12" scores 0.60 while
being perfectly good text. Length thresholds are named constants because they
are tuning knobs, not laws; see the note in DESIGN.md about tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from updater.ingest.errors import EXTRACTION_FAILED_STATUS, ExtractionFailed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from updater.models import Passage, Source

__all__ = [
    "ALNUM_RATIO_FLOOR",
    "MAX_TOKEN_LENGTH",
    "RATIO_CHECK_MIN_CHARS",
    "QualityFailure",
    "QualityIssue",
    "alnum_ratio",
    "apply_quality_gate",
    "check_passage",
    "is_addressable",
    "longest_token",
]

LOGGER = logging.getLogger(__name__)

ALNUM_RATIO_FLOOR = 0.6
"""Minimum share of characters that must be alphanumeric."""

MAX_TOKEN_LENGTH = 50
"""Longest acceptable whitespace-delimited run of characters.

Not applied to URLs and email addresses; see :func:`is_addressable`.
"""

RATIO_CHECK_MIN_CHARS = 30
"""Below this length the alphanumeric ratio is too noisy to judge."""


class QualityIssue(StrEnum):
    """Why a passage was rejected. Shown to the reviewer as-is."""

    EMPTY = "empty"
    OVERLONG_TOKEN = "overlong-token"
    LOW_ALNUM_RATIO = "low-alnum-ratio"


@dataclass(frozen=True, slots=True)
class QualityFailure:
    """One rejected passage, named and diagnosed.

    Kept granular on purpose: "this document is bad" is not actionable, whereas
    "page 7 is 31% alphanumeric" tells a reviewer to go and look at page 7.
    """

    passage_id: str
    issue: QualityIssue
    detail: str

    def __str__(self) -> str:
        return f"{self.passage_id} [{self.issue}] {self.detail}"


def alnum_ratio(text: str) -> float:
    """Share of characters that are alphanumeric, counting whitespace as not.

    ``str.isalnum`` is Unicode-aware, so accented and non-Latin scripts are not
    penalised — only symbol soup is.
    """
    if not text:
        return 0.0
    return sum(1 for character in text if character.isalnum()) / len(text)


_URL_PREFIXES = ("http://", "https://", "www.", "doi:", "ftp://")


def is_addressable(token: str) -> bool:
    """Whether a long token is a URL or an email rather than a failure.

    The overlong-token check exists to catch ligature and encoding failures —
    the ones that smear a whole paragraph into a single unbroken run. A URL is
    not that. It is long *and structured*, and it is exactly what a
    bibliography is full of.

    This was found on real ASIL material: a 176-character link to an Australian
    Senate committee report, in the sources line of
    ``ASIL_Enterprise_Skills_Report_2026.docx``, failed the whole 161-passage
    document. Rejecting a research report because it cites its sources properly
    is the check working against the thing it is meant to protect.

    The fail-the-whole-document policy is unchanged and still right (see
    DESIGN.md, 2026-08-21). What was wrong was the check, not the consequence.
    """
    lowered = token.casefold()
    if lowered.startswith(_URL_PREFIXES):
        return True
    # An email: exactly one @, with something either side and a dot after it.
    local, at, domain = token.partition("@")
    return bool(at) and bool(local) and "." in domain and "@" not in domain


def longest_token(text: str) -> str:
    """The longest whitespace-delimited run in ``text``, or ``""``.

    URLs and email addresses are skipped rather than measured — they are long
    by nature, and this check is looking for text that has lost its spaces.
    """
    candidates = [token for token in text.split() if not is_addressable(token)]
    return max(candidates, key=len, default="")


def check_passage(passage: Passage) -> QualityFailure | None:
    """Judge one passage. Returns ``None`` when it is acceptable."""
    text = passage.text

    if not text.strip():
        return QualityFailure(
            passage.id, QualityIssue.EMPTY, "no non-whitespace characters"
        )

    token = longest_token(text)
    if len(token) > MAX_TOKEN_LENGTH:
        return QualityFailure(
            passage.id,
            QualityIssue.OVERLONG_TOKEN,
            f"{len(token)}-character run, limit {MAX_TOKEN_LENGTH}: {token[:60]!r}",
        )

    if len(text) >= RATIO_CHECK_MIN_CHARS:
        ratio = alnum_ratio(text)
        if ratio < ALNUM_RATIO_FLOOR:
            return QualityFailure(
                passage.id,
                QualityIssue.LOW_ALNUM_RATIO,
                f"{ratio:.0%} alphanumeric, floor is {ALNUM_RATIO_FLOOR:.0%}",
            )

    return None


def apply_quality_gate(
    passages: Iterable[Passage],
    *,
    source: Source,
    path: Path,
) -> list[Passage]:
    """Accept an adapter's output, or fail loudly with per-passage reasons.

    Applied centrally by the registry rather than inside each adapter, so a new
    adapter cannot forget to run it.
    """
    accepted = list(passages)

    if not accepted:
        raise ExtractionFailed(
            path=path, source_id=source.id, reason="produced no passages"
        )

    failures = [
        failure
        for failure in (check_passage(passage) for passage in accepted)
        if failure is not None
    ]
    if failures:
        for failure in failures:
            LOGGER.warning(
                "%s: source %s: %s", EXTRACTION_FAILED_STATUS, source.id, failure
            )
        raise ExtractionFailed(
            path=path,
            source_id=source.id,
            reason=(
                f"failed the extraction quality gate on {len(failures)} of "
                f"{len(accepted)} passage(s)"
            ),
            failures=failures,
        )

    return accepted
