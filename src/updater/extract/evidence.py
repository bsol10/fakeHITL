"""Evidence extraction: one model call per claim, then verification of every quote.

BUILD_SPEC.md section 4, ``extract_evidence``, and BACKEND_PLAN.md phase 5. The
model is shown one claim and the passages retrieval found for it, and asked for
structured findings. It may author only (phase 5, verbatim): the finding text, a
type, a quoted span, the id of the passage it quoted, and nullable statistic,
date, caveat and method fields. **It never authors a source, a citation or a
page reference.** The anchor on each :class:`~updater.models.Evidence` comes
from :func:`updater.extract.verify.verify_quote`, which finds the quote in the
passage the model named and narrows that passage's anchor to it. A field the
model adds beyond the list — a ``source``, a ``page`` — makes the finding
malformed, and it is discarded.

What is discarded, and why each one is counted rather than dropped quietly:

``quote-not-found``
    The quote is not in the passage it cites, after the exact and the
    whitespace-flexible pass. A fabricated quote, a near miss, a quote from a
    different passage than the one named.
``statistic-not-in-quote``
    The statistic carries a number that its own verified quote does not. A
    model that computes "a rise of 1.7 points" from two figures, or rounds
    13.14% to 13.1%, has authored a number, and rule 2 says a number nobody
    printed is a number nobody may cite.
``unknown-passage``
    The passage id is not one of those supplied. The schema's enum makes this
    impossible for a provider that honours it, so seeing it means one did not.
``malformed``
    Missing or extra fields, a wrong type, an empty finding, a date that is not
    ISO. The schema forbids all of these too.

A discarded finding is logged and returned in :attr:`ExtractionResult.discarded`,
so the count can reach the reviewer: "four findings, two discarded" and "two
findings" are different statements about how much to trust a run.

Two outcomes are *not* an empty result. No passages means no call: there is
nothing to extract from, which is the compare stage's ``cannot-verify``, and
asking a model anyway would invite an answer from its training data (rule 3).
And a reply that is not the requested JSON at all raises
:class:`~updater.extract.model.ModelCallFailed`, because an empty evidence list
would read as "the sources do not support this claim", which nobody found out.

``Evidence.confidence`` is **not** the model's opinion, which phase 5 does not
let it author. It records how the quote verified: :data:`EXACT_QUOTE_CONFIDENCE`
for a character-for-character match, :data:`WHITESPACE_QUOTE_CONFIDENCE` when
the whitespace had to be relaxed. Phase 6 owns the claim-level ``conf``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from updater.extract.model import ModelCallFailed
from updater.extract.verify import verify_quote
from updater.models import Evidence

if TYPE_CHECKING:
    from updater.extract.model import EvidenceModel
    from updater.models import Passage

__all__ = [
    "EXACT_QUOTE_CONFIDENCE",
    "SYSTEM_PROMPT",
    "WHITESPACE_QUOTE_CONFIDENCE",
    "DiscardReason",
    "DiscardedFinding",
    "ExtractedEvidence",
    "ExtractionResult",
    "FindingType",
    "build_prompt",
    "evidence_schema",
    "extract_evidence",
]

LOGGER = logging.getLogger(__name__)

EXACT_QUOTE_CONFIDENCE = 1.0
"""The quote is in the passage character for character."""
WHITESPACE_QUOTE_CONFIDENCE = 0.9
"""The quote's words are in the passage in order, but its whitespace was not.
The characters shown are the document's either way; this records that the
model did not copy them exactly, which is worth a reviewer knowing."""


class FindingType(StrEnum):
    """What a finding is. The prototype's claim ``type``, lower-cased like the
    package's other enums; the API layer owns the display labels."""

    STATISTIC = "statistic"
    FINDING = "finding"
    CAVEAT = "caveat"
    DATE = "date"
    METHOD = "method"


class DiscardReason(StrEnum):
    """Why a finding the model returned did not become evidence."""

    QUOTE_NOT_FOUND = "quote-not-found"
    STATISTIC_NOT_IN_QUOTE = "statistic-not-in-quote"
    UNKNOWN_PASSAGE = "unknown-passage"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedEvidence:
    """One verified finding, with what it was found in."""

    evidence: Evidence
    type: FindingType
    passage: Passage
    """The passage the quote was verified against."""
    exact: bool
    """Whether the quote matched character for character."""

    @property
    def quote(self) -> str:
        """The supporting span, sliced from the passage at the evidence anchor.

        Derived, never stored (DESIGN.md, 2026-08-20: no ``quote`` field on
        ``Evidence``), so it cannot drift from the anchor that justifies it.
        These are the document's characters, whatever the model typed.
        """
        offset = self.passage.anchor.char_start
        anchor = self.evidence.anchor
        return self.passage.text[anchor.char_start - offset : anchor.char_end - offset]


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscardedFinding:
    """A finding the model returned that did not survive verification."""

    reason: DiscardReason
    detail: str
    item: object
    """What the model returned for this finding, kept for whoever debugs it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionResult:
    """Everything one claim's extraction produced, kept and discarded alike."""

    evidence: tuple[ExtractedEvidence, ...]
    discarded: tuple[DiscardedFinding, ...]
    model: str | None
    """The model the API reported, or None when no call was made because there
    were no passages to read."""

    @property
    def discarded_count(self) -> int:
        return len(self.discarded)


SYSTEM_PROMPT = """\
You extract evidence about one claim from a fixed set of source passages, for a \
research report that a person will check against those sources.

Rules:
1. Use only the passages supplied. Do not use anything else you know, even if \
you are sure it is true. If it is not written in a passage, it is not evidence.
2. Every finding quotes a span copied character for character from one passage, \
and gives that passage's id. The quote is checked by exact string match: a \
paraphrased, corrected, shortened or rejoined quote is thrown away.
3. Quote the shortest span that supports the finding. Never join text from two \
passages into one quote.
4. statistic, method, as_of_date and caveat are null unless the quoted passage \
states them. Never estimate, round, convert, compute or infer a value. A number \
that is not written in the quote is not a statistic.
5. as_of_date is an ISO date (YYYY-MM-DD) only when the passage states a full \
calendar date. If it gives only a year or a month, as_of_date is null; put the \
period in the finding text instead. Never fill in a missing day or month.
6. If no passage bears on the claim, return an empty evidence list. That is a \
correct and useful answer, not a failure.
7. Do not name sources, page numbers or citations. The system attaches those \
from the passage id."""

_REQUIRED_TEXT = ("passage_id", "quote", "type", "finding")
_NULLABLE_TEXT = ("statistic", "method", "as_of_date", "caveat")
_FIELDS = frozenset(_REQUIRED_TEXT + _NULLABLE_TEXT)

_NUMBER = re.compile(r"\d+")


def evidence_schema(passage_ids: Sequence[str]) -> dict[str, Any]:
    """The JSON schema a reply must satisfy, for this call's passages.

    ``passage_id`` is an enum of exactly the ids supplied, so a provider that
    honours the schema cannot cite a passage it was not shown. Only keywords
    both providers document as supported are used: ``type``, ``enum``,
    ``anyOf``, ``format``, ``properties``, ``required``,
    ``additionalProperties`` and ``description``.
    """
    nullable_text = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "passage_id": {
                            "type": "string",
                            "enum": list(passage_ids),
                            "description": "The id of the passage quoted.",
                        },
                        "quote": {
                            "type": "string",
                            "description": "A span copied verbatim from it.",
                        },
                        "type": {
                            "type": "string",
                            "enum": [kind.value for kind in FindingType],
                        },
                        "finding": {
                            "type": "string",
                            "description": "What the quote says, in one sentence.",
                        },
                        "statistic": {
                            **nullable_text,
                            "description": "The figure as written, or null.",
                        },
                        "method": nullable_text,
                        "as_of_date": {
                            "anyOf": [
                                {"type": "string", "format": "date"},
                                {"type": "null"},
                            ]
                        },
                        "caveat": nullable_text,
                    },
                    "required": [*_REQUIRED_TEXT, *_NULLABLE_TEXT],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["evidence"],
        "additionalProperties": False,
    }


def build_prompt(claim_text: str, passages: Sequence[Passage]) -> str:
    """The user message: the claim, then every passage under its id."""
    blocks = "\n\n".join(
        f'<passage id="{passage.id}">\n{passage.text}\n</passage>'
        for passage in passages
    )
    return (
        "Claim from last year's report:\n"
        f"<claim>\n{claim_text}\n</claim>\n\n"
        "Passages, the only material you may use:\n\n"
        f"{blocks}\n\n"
        "Return every finding in these passages that corroborates, contradicts, "
        "updates or qualifies the claim."
    )


def extract_evidence(
    claim_text: str, passages: Sequence[Passage], *, model: EvidenceModel
) -> ExtractionResult:
    """Ask ``model`` for evidence about one claim, and keep what verifies.

    Takes the claim's text rather than a :class:`~updater.models.Claim`, which
    needs an anchor into last year's report: a claim typed at the command line
    has none, and minting one would be a citation pointing nowhere.
    """
    by_id: dict[str, Passage] = {}
    for passage in passages:
        if passage.id in by_id:
            raise ValueError(f"two passages share the id {passage.id!r}")
        by_id[passage.id] = passage
    if not by_id:
        return ExtractionResult(evidence=(), discarded=(), model=None)

    reply = model.complete(
        system=SYSTEM_PROMPT,
        prompt=build_prompt(claim_text, passages),
        schema=evidence_schema(list(by_id)),
    )
    kept: list[ExtractedEvidence] = []
    discarded: list[DiscardedFinding] = []
    for item in _reply_items(reply.text, reply.model):
        outcome = _check(item, by_id)
        if isinstance(outcome, DiscardedFinding):
            LOGGER.warning(
                "discarded a finding (%s): %s", outcome.reason, outcome.detail
            )
            discarded.append(outcome)
        else:
            kept.append(outcome)
    return ExtractionResult(
        evidence=tuple(kept), discarded=tuple(discarded), model=reply.model
    )


def _reply_items(text: str, model: str) -> list[object]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelCallFailed(
            f"{model} replied with text that is not JSON: {error}"
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
        raise ModelCallFailed(
            f"{model} replied with JSON that is not the requested shape: expected "
            "an object holding an 'evidence' list"
        )
    items: list[object] = data["evidence"]
    return items


def _discard(reason: DiscardReason, detail: str, item: object) -> DiscardedFinding:
    return DiscardedFinding(reason=reason, detail=detail, item=item)


def _check(
    item: object, passages: Mapping[str, Passage]
) -> ExtractedEvidence | DiscardedFinding:
    """Turn one reply item into verified evidence, or say why it cannot be."""
    malformed = DiscardReason.MALFORMED
    if not isinstance(item, dict):
        return _discard(malformed, "a finding that is not a JSON object", item)
    missing = sorted(_FIELDS - item.keys())
    unknown = sorted(str(key) for key in item.keys() - _FIELDS)
    if missing or unknown:
        return _discard(
            malformed, f"missing fields {missing}, unknown fields {unknown}", item
        )
    for key in _REQUIRED_TEXT:
        if not isinstance(item[key], str) or not item[key].strip():
            return _discard(malformed, f"{key} is not a non-empty string", item)
    for key in _NULLABLE_TEXT:
        if item[key] is not None and not isinstance(item[key], str):
            return _discard(malformed, f"{key} is neither a string nor null", item)
    try:
        kind = FindingType(item["type"])
    except ValueError:
        return _discard(malformed, f"type {item['type']!r} is not a known type", item)
    statistic, method, caveat, as_of = (
        _text_or_none(item[key])
        for key in ("statistic", "method", "caveat", "as_of_date")
    )
    try:
        as_of_date = None if as_of is None else date.fromisoformat(as_of)
    except ValueError:
        return _discard(malformed, f"as_of_date {as_of!r} is not an ISO date", item)

    passage = passages.get(item["passage_id"])
    if passage is None:
        return _discard(
            DiscardReason.UNKNOWN_PASSAGE,
            f"passage {item['passage_id']!r} was not among those supplied",
            item,
        )
    span = verify_quote(passage, item["quote"])
    if span is None:
        return _discard(
            DiscardReason.QUOTE_NOT_FOUND,
            f"quote {item['quote']!r} is not in passage {passage.id!r}",
            item,
        )
    if statistic is not None:
        printed = set(_NUMBER.findall(span.text))
        unprinted = sorted(set(_NUMBER.findall(statistic)) - printed)
        if unprinted:
            return _discard(
                DiscardReason.STATISTIC_NOT_IN_QUOTE,
                f"statistic {statistic!r} has numbers {unprinted} that its quote "
                f"{span.text!r} does not",
                item,
            )

    evidence = Evidence(
        finding=item["finding"].strip(),
        statistic=statistic,
        method=method,
        as_of_date=as_of_date,
        caveat=caveat,
        anchor=span.anchor,
        confidence=(
            EXACT_QUOTE_CONFIDENCE if span.exact else WHITESPACE_QUOTE_CONFIDENCE
        ),
    )
    return ExtractedEvidence(
        evidence=evidence, type=kind, passage=passage, exact=span.exact
    )


def _text_or_none(value: object) -> str | None:
    """A nullable field: blank is null, since it states nothing either way."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
