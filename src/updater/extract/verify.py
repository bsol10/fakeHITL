"""Verbatim-span verification: does the quoted text really appear in the passage?

BUILD_SPEC.md section 4 calls this "a cheap, extremely effective hallucination
guard — implement it, do not skip it", and DESIGN.md lists skipping it among
the things the design rules rule out. It is a pure function in a module of its
own that imports no provider (BACKEND_PLAN.md phase 5), so it can be read,
tested and trusted without a model anywhere near it.

Two passes, and only two:

1. **Exact.** The quote, character for character.
2. **Whitespace-flexible, and nothing else.** The quote's words must appear in
   the same order with *some* whitespace between each pair. This forgives a
   model that wrote one space where the document has a line break or two
   spaces — common in text extracted from PDFs and table cells. It does not
   forgive a changed digit, a straightened quote mark, a corrected typo or a
   dropped word, because each of those is the model's text rather than the
   document's, and the citation would be vouching for words the source never
   printed.

Both passes match **whole tokens only**: a quote that starts or ends on a
letter or digit must not continue a word in the passage. Without that, ``3.1%``
verifies against a passage reading ``13.1%`` — a one-digit near miss that is,
character for character, a substring of the truth.

Whichever pass matches, the span returned is **the document's characters**, not
the model's quote. What a reviewer reads beside a finding is the source.

Offsets are **locator-relative**, like every anchor in the system: a match at
index ``i`` of ``passage.text`` is at ``passage.anchor.char_start + i`` in the
locator unit. A chunk that starts 300 characters into its page has
``char_start == 300``, and forgetting the addition would point every citation
from that chunk 300 characters early, at text that is really there — the kind of
wrong that reads perfectly well. :func:`require_derives_within` checks the
result against the passage's own anchor before it is returned.

A quote that appears more than once is anchored at its first occurrence. The
characters at both places are identical, so the citation is true either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from updater.models import SourceAnchor
from updater.provenance import require_derives_within

if TYPE_CHECKING:
    from updater.models import Passage

__all__ = ["VerifiedSpan", "locate_quote", "verify_quote"]

_WORD_CHARACTER = re.compile(r"\w")


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedSpan:
    """Where a quote was found, anchored into the locator unit."""

    anchor: SourceAnchor
    text: str
    """The passage's own characters at ``anchor`` — never the model's quote."""
    exact: bool
    """False when the match needed the whitespace-flexible pass."""


def _whole_tokens(pattern: str, first: str, last: str) -> str:
    """Refuse a match that starts or ends in the middle of a word or number."""
    head = r"(?<!\w)" if _WORD_CHARACTER.match(first) else ""
    tail = r"(?!\w)" if _WORD_CHARACTER.match(last) else ""
    return f"{head}{pattern}{tail}"


def locate_quote(quote: str, text: str) -> tuple[int, int, bool] | None:
    """``(start, end, exact)`` of ``quote`` in ``text``, or None.

    Indices are into ``text``. A quote with no visible characters matches
    nothing: an empty span would verify against every passage there is.
    """
    words = quote.split()
    if not words:
        return None
    passes = (
        (_whole_tokens(re.escape(quote), quote[0], quote[-1]), True),
        (
            _whole_tokens(
                r"\s+".join(re.escape(word) for word in words),
                words[0][0],
                words[-1][-1],
            ),
            False,
        ),
    )
    for pattern, exact in passes:
        match = re.search(pattern, text)
        if match is not None:
            return match.start(), match.end(), exact
    return None


def verify_quote(passage: Passage, quote: str) -> VerifiedSpan | None:
    """Anchor ``quote`` inside ``passage``, or return None if it is not there."""
    found = locate_quote(quote, passage.text)
    if found is None:
        return None
    start, end, exact = found
    base = passage.anchor.char_start
    anchor = SourceAnchor(
        source_id=passage.anchor.source_id,
        locator=passage.anchor.locator,
        char_start=base + start,
        char_end=base + end,
        extracted_at=passage.anchor.extracted_at,
    )
    require_derives_within(passage.anchor, anchor)
    return VerifiedSpan(anchor=anchor, text=passage.text[start:end], exact=exact)
