"""Verbatim-span verification: does the quoted text really appear in the passage?

The hallucination guard of BUILD_SPEC.md section 4. Pure, and imports no
provider, so it can be tested without a model.

Two passes, and only two:

1. **Exact**, character for character.
2. **Whitespace-flexible, and nothing else**: the quote's words in order with
   any whitespace between them, forgiving one space where the document has a
   line break, as PDF and table-cell text often does. A changed digit, a
   straightened quote mark, a corrected typo or a dropped word is not forgiven:
   the citation would vouch for words the source never printed.

Both passes match **whole tokens only**, or ``3.1%`` would verify against a
passage reading ``13.1%``.

The span returned is always the document's characters, never the model's
quote. Offsets are **locator-relative**: a match at index ``i`` of
``passage.text`` is at ``passage.anchor.char_start + i``. Forgetting the
addition would point every citation from a chunk that starts mid-page at the
wrong text, and still read well. :func:`require_derives_within` checks the
result against the passage's anchor.

A repeated quote is anchored at its first occurrence; the characters at each
are identical, so the citation is true either way.
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
