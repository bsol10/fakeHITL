"""Evidence extraction and quote verification, with a scripted model.

Like the chunking tests, these assert absence as well as presence: a discarded
finding must be *gone from the evidence and present in the count*. A discard
that only disappears is a silent loss; one that is only counted is a fabricated
quote in the draft.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

import pytest

from conftest import (
    SCRIPTED_ANSWERED_BY,
    ScriptedModel,
    evidence_reply,
    finding,
    make_passage,
)
from updater.extract import (
    EXACT_QUOTE_CONFIDENCE,
    WHITESPACE_QUOTE_CONFIDENCE,
    DiscardReason,
    ExtractionResult,
    ModelCallFailed,
    evidence_schema,
    extract_evidence,
    locate_quote,
)
from updater.models import Passage
from updater.provenance import derives_within

DESTINATIONS = (
    "Sustained destinations rose to 68% of the 2024 leaver cohort, measured "
    "six months after the end of key stage 4."
)
CLAIM = "Sustained destinations were 64% of the leaver cohort."


def extract(passages: list[Passage], *items: object) -> ExtractionResult:
    return extract_evidence(
        CLAIM, passages, model=ScriptedModel(evidence_reply(*items))
    )


def only_discard(result: ExtractionResult) -> DiscardReason:
    assert result.evidence == (), "a discarded finding must not also be kept"
    assert result.discarded_count == 1
    return result.discarded[0].reason


# The five tests BACKEND_PLAN.md phase 5 asks for


def test_a_fabricated_quote_is_discarded_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    passage = make_passage(DESTINATIONS)
    with caplog.at_level(logging.WARNING, logger="updater.extract"):
        result = extract(
            [passage],
            finding(quote="rose to 71% of the 2024 leaver cohort", statistic="71%"),
        )

    assert only_discard(result) is DiscardReason.QUOTE_NOT_FOUND
    assert "71%" in caplog.text, "a discard is logged, not only counted"


@pytest.mark.parametrize(
    "quote",
    [
        "rose to 69% of the 2024 leaver cohort",  # one digit changed
        "8% of the 2024 leaver cohort",  # one digit dropped from the front
        "rose to 68% of the 2025 leaver cohort",  # the year moved by one
        "6.8% of the 2024 leaver cohort",  # a decimal point inserted
    ],
    ids=["changed", "dropped-leading-digit", "year", "decimal-point"],
)
def test_a_one_digit_near_miss_is_discarded(quote: str) -> None:
    """``8% of the 2024`` *is* a substring of the passage, starting inside
    ``68%``. Only the whole-token rule stops it verifying."""
    result = extract([make_passage(DESTINATIONS)], finding(quote=quote))

    assert only_discard(result) is DiscardReason.QUOTE_NOT_FOUND


def test_the_quote_shown_is_the_documents_characters() -> None:
    """The model normalised a line break and a double space to single spaces.
    That verifies, and what is shown is the document's text, not the model's."""
    text = "Sustained destinations rose to\n68%  of the 2024 leaver cohort."
    passage = make_passage(text)

    result = extract([passage], finding(quote="rose to 68% of the 2024 leaver"))

    [found] = result.evidence
    assert found.quote == "rose to\n68%  of the 2024 leaver"
    assert found.quote in text
    assert not found.exact
    assert found.evidence.confidence == WHITESPACE_QUOTE_CONFIDENCE


def test_a_missing_statistic_stays_null() -> None:
    """The most important property in the project (PROJECT_OVERVIEW.md
    section 15): nothing stated, nothing invented."""
    method = make_passage(
        "Figures are drawn from matched administrative records covering all "
        "state-funded schools in England."
    )

    result = extract(
        [method],
        finding(
            quote="matched administrative records",
            type="method",
            finding="Figures come from matched administrative records.",
        ),
    )

    [found] = result.evidence
    assert found.evidence.statistic is None
    assert found.evidence.as_of_date is None
    assert found.evidence.caveat is None
    assert found.evidence.method is None


def test_offsets_index_the_locator_unit_not_the_passage() -> None:
    """A chunk starting 300 characters into its page. The quote's anchor must
    point into the *page*: a hit at index i of the chunk is at 300 + i."""
    page = ("Preceding text on the page. " * 11)[:300] + DESTINATIONS
    chunk = make_passage(DESTINATIONS, char_start=300, char_end=300 + len(DESTINATIONS))

    result = extract([chunk], finding(quote="68%", statistic="68%"))

    [found] = result.evidence
    anchor = found.evidence.anchor
    assert anchor.char_start == 300 + DESTINATIONS.index("68%")
    assert page[anchor.char_start : anchor.char_end] == "68%"
    assert found.quote == "68%"
    assert derives_within(chunk.anchor, anchor)


# What a verified finding carries


def test_a_verbatim_quote_becomes_evidence_anchored_at_its_span() -> None:
    passage = make_passage(DESTINATIONS)
    quote = "rose to 68% of the 2024 leaver cohort"

    result = extract(
        [passage], finding(quote=quote, statistic="68%", finding="Rose to 68%.")
    )

    assert result.discarded == ()
    [found] = result.evidence
    start = DESTINATIONS.index(quote)
    anchor = found.evidence.anchor
    assert (anchor.char_start, anchor.char_end) == (start, start + len(quote))
    assert anchor.source_id == passage.anchor.source_id
    assert anchor.locator == passage.anchor.locator
    assert found.quote == quote
    assert found.exact
    assert found.evidence.confidence == EXACT_QUOTE_CONFIDENCE
    assert found.evidence.statistic == "68%"
    assert found.passage is passage


def test_the_result_records_the_model_the_reply_names() -> None:
    model = ScriptedModel(evidence_reply())

    result = extract_evidence(CLAIM, [make_passage(DESTINATIONS)], model=model)

    assert result.model == SCRIPTED_ANSWERED_BY
    assert result.model != model.model


def test_a_blank_field_is_null_not_an_empty_string() -> None:
    result = extract(
        [make_passage(DESTINATIONS)],
        finding(quote="68%", statistic="68%", caveat="  ", method=""),
    )

    [found] = result.evidence
    assert found.evidence.caveat is None
    assert found.evidence.method is None


def test_a_stated_full_date_is_kept_as_a_date() -> None:
    passage = make_passage("Figures as at 30 June 2025 show 68% sustained.")

    result = extract(
        [passage], finding(quote="as at 30 June 2025", as_of_date="2025-06-30")
    )

    [found] = result.evidence
    assert found.evidence.as_of_date is not None
    assert found.evidence.as_of_date.isoformat() == "2025-06-30"


def test_a_repeated_quote_is_anchored_at_its_first_occurrence() -> None:
    text = "68% in 2024. Last year it was also 68% in 2024."
    result = extract([make_passage(text)], finding(quote="68% in 2024"))

    [found] = result.evidence
    assert found.evidence.anchor.char_start == 0
    assert found.quote == "68% in 2024"


# What the model may not author


@pytest.mark.parametrize(
    ("quote", "statistic"),
    [
        ("Sustained destinations rose", "68%"),  # the figure is outside the quote
        ("rose to 68% of the 2024 leaver cohort", "4 points"),  # computed
        ("rose to 68% of the 2024 leaver cohort", "68.0%"),  # precision invented
    ],
    ids=["outside-quote", "computed", "invented-precision"],
)
def test_a_statistic_its_quote_does_not_print_is_discarded(
    quote: str, statistic: str
) -> None:
    result = extract(
        [make_passage(DESTINATIONS)], finding(quote=quote, statistic=statistic)
    )

    assert only_discard(result) is DiscardReason.STATISTIC_NOT_IN_QUOTE


def test_a_quote_from_a_passage_other_than_the_one_named_is_discarded() -> None:
    destinations = make_passage(DESTINATIONS, passage_id="src:page7")
    method = make_passage(
        "A destination counts as sustained after two terms.",
        passage_id="src:page8",
        locator=None,
    )

    result = extract(
        [destinations, method],
        finding(passage_id="src:page8", quote="rose to 68%", statistic="68%"),
    )

    assert only_discard(result) is DiscardReason.QUOTE_NOT_FOUND


def test_a_passage_id_that_was_not_supplied_is_discarded() -> None:
    result = extract(
        [make_passage(DESTINATIONS)], finding(passage_id="src:page99", quote="68%")
    )

    assert only_discard(result) is DiscardReason.UNKNOWN_PASSAGE


@pytest.mark.parametrize("extra", ["page", "source", "citation"])
def test_a_finding_that_authors_a_citation_is_discarded(extra: str) -> None:
    """The model never authors a source, a citation or a page."""
    result = extract(
        [make_passage(DESTINATIONS)], finding(quote="68%", **{extra: "p. 7"})
    )

    assert only_discard(result) is DiscardReason.MALFORMED


@pytest.mark.parametrize(
    "broken",
    [
        {"finding": "   "},
        {"type": "opinion"},
        {"statistic": 68},
        {"as_of_date": "June 2025"},
        {"quote": None},
    ],
    ids=[
        "empty-finding",
        "unknown-type",
        "number-not-string",
        "partial-date",
        "no-quote",
    ],
)
def test_a_malformed_finding_is_discarded_and_the_rest_are_kept(
    broken: dict[str, Any],
) -> None:
    good = finding(quote="68%", statistic="68%")

    result = extract(
        [make_passage(DESTINATIONS)], finding(**{"quote": "68%", **broken}), good
    )

    assert len(result.evidence) == 1
    assert [d.reason for d in result.discarded] == [DiscardReason.MALFORMED]


def test_a_finding_missing_a_field_is_discarded() -> None:
    incomplete = finding(quote="68%")
    del incomplete["caveat"]

    result = extract([make_passage(DESTINATIONS)], incomplete)

    assert only_discard(result) is DiscardReason.MALFORMED


# Never an empty result by accident


def test_no_passages_means_no_model_call() -> None:
    """Nothing retrieved is cannot-verify. Asking the model anyway would invite
    an answer from its training data (rule 3)."""
    model = ScriptedModel(evidence_reply(finding(quote="68%")))

    result = extract_evidence(CLAIM, [], model=model)

    assert model.calls == []
    assert result.evidence == ()
    assert result.model is None


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "[]",
        json.dumps({"findings": []}),
        json.dumps({"evidence": {}}),
    ],
    ids=["not-json", "a-list", "wrong-key", "evidence-not-a-list"],
)
def test_a_reply_that_is_not_the_requested_json_raises(reply: str) -> None:
    """An empty evidence list here would read as "the sources do not support
    this claim", which nobody found out."""
    with pytest.raises(ModelCallFailed):
        extract_evidence(
            CLAIM, [make_passage(DESTINATIONS)], model=ScriptedModel(reply)
        )


def test_two_passages_sharing_an_id_are_refused() -> None:
    twice = [make_passage(DESTINATIONS), make_passage("Other text.")]

    with pytest.raises(ValueError, match="share the id"):
        extract(twice)


def test_an_empty_quote_verifies_against_nothing() -> None:
    assert locate_quote("", DESTINATIONS) is None
    assert locate_quote(" \n ", DESTINATIONS) is None


# The request


def test_the_prompt_holds_the_claim_and_only_the_supplied_passages() -> None:
    passages = [
        make_passage(DESTINATIONS, passage_id="dfe:p7"),
        make_passage("Coverage is incomplete.", passage_id="dfe:p9"),
    ]
    model = ScriptedModel(evidence_reply())

    extract_evidence(CLAIM, passages, model=model)

    [call] = model.calls
    assert CLAIM in call["prompt"]
    for passage in passages:
        assert (
            f'<passage id="{passage.id}">\n{passage.text}\n</passage>' in call["prompt"]
        )
    assert call["prompt"].count("<passage id=") == 2
    system = call["system"].lower()
    for rule in ("only the passages", "character for character", "null", "empty"):
        assert rule in system
    item = call["schema"]["properties"]["evidence"]["items"]
    assert item["properties"]["passage_id"]["enum"] == ["dfe:p7", "dfe:p9"]


def test_the_schema_is_one_anthropic_accepts_unchanged() -> None:
    """``anthropic.transform_schema`` rewrites whatever structured outputs does
    not support. A schema it leaves alone is one Claude takes as written."""
    import anthropic

    schema = evidence_schema(["a", "b"])

    assert anthropic.transform_schema(schema) == schema


GEMINI_SCHEMA_KEYWORDS = frozenset(
    {
        "$id", "$defs", "$ref", "$anchor", "type", "format", "title",
        "description", "enum", "items", "prefixItems", "minItems", "maxItems",
        "minimum", "maximum", "anyOf", "oneOf", "properties",
        "additionalProperties", "required", "propertyOrdering",
    }
)  # fmt: skip
"""The keywords google-genai 2.29 documents for ``response_json_schema``."""


def _keywords(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key == "properties":
                for child in value.values():
                    found |= _keywords(child)
            else:
                found |= _keywords(value)
    elif isinstance(node, list):
        for child in node:
            found |= _keywords(child)
    return found


def test_the_schema_uses_only_keywords_gemini_supports() -> None:
    assert _keywords(evidence_schema(["a"])) <= GEMINI_SCHEMA_KEYWORDS


def test_importing_extraction_loads_no_provider_sdk() -> None:
    """Each provider SDK is loaded only when its provider is chosen."""
    code = (
        "import sys, updater.extract.verify, updater.extract.evidence\n"
        "loaded = [m for m in ('anthropic', 'google.genai') if m in sys.modules]\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert run.returncode == 0, run.stdout + run.stderr
