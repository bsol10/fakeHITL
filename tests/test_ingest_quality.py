"""Extraction quality gate tests (BUILD_SPEC.md section 3).

The gate exists so that a garbled source fails loudly instead of contributing
zero passages. Downstream, "no passages" and "this source says nothing about
that claim" are indistinguishable, and only one of them is a finding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import make_source
from updater.ingest import (
    MAX_TOKEN_LENGTH,
    ExtractionFailed,
    QualityIssue,
    apply_quality_gate,
    check_passage,
)
from updater.models import PageLocator, Passage, SourceAnchor

CLEAN_PROSE = (
    "Sustained destinations rose to 68% of the 2024 leaver cohort, measured "
    "six months after the end of key stage 4."
)

OCR_GARBAGE = "~ | ' ' , . : ; -- // ** ~~ * - . , ' ; : | ~ - . ' , : ; //"

LIGATURE_SMEAR = "Sustained destinationsroseto68percentofthecohortmeasuredsixmonths"


def make_passage(text: str, *, passage_id: str = "src:page1") -> Passage:
    anchor = SourceAnchor(
        source_id="src",
        locator=PageLocator(page=1),
        char_start=0,
        char_end=len(text),
        extracted_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    return Passage(
        id=passage_id, source_id="src", text=text, anchor=anchor, token_count=1
    )


def test_clean_prose_passes() -> None:
    assert check_passage(make_passage(CLEAN_PROSE)) is None


def test_ocr_garbage_fails_on_the_alphanumeric_ratio() -> None:
    failure = check_passage(make_passage(OCR_GARBAGE))

    assert failure is not None
    assert failure.issue is QualityIssue.LOW_ALNUM_RATIO


def test_a_ligature_smear_fails_on_token_length() -> None:
    """One unbroken run means the encoding lost its spaces, not that it is prose."""
    failure = check_passage(make_passage(LIGATURE_SMEAR))

    assert failure is not None
    assert failure.issue is QualityIssue.OVERLONG_TOKEN


def test_whitespace_only_text_fails_as_empty() -> None:
    failure = check_passage(make_passage("   \n\t  "))

    assert failure is not None
    assert failure.issue is QualityIssue.EMPTY


def test_a_short_fragment_is_not_judged_on_ratio() -> None:
    """A citation like "(p. 12)" is 43% alphanumeric and perfectly good text."""
    assert check_passage(make_passage("(p. 12)")) is None


def test_the_gate_names_the_failing_passage(tmp_path: Path) -> None:
    source = make_source()
    passages = [
        make_passage(CLEAN_PROSE, passage_id="src:page1"),
        make_passage(OCR_GARBAGE, passage_id="src:page2"),
    ]

    with pytest.raises(ExtractionFailed) as caught:
        apply_quality_gate(passages, source=source, path=tmp_path / "report.pdf")

    error = caught.value
    assert error.status == "extraction-failed"
    assert [failure.passage_id for failure in error.failures] == ["src:page2"]
    assert "1 of 2 passage(s)" in str(error)


def test_the_gate_rejects_an_empty_extraction(tmp_path: Path) -> None:
    with pytest.raises(ExtractionFailed, match="produced no passages"):
        apply_quality_gate([], source=make_source(), path=tmp_path / "report.pdf")


def test_clean_passages_come_back_unchanged(tmp_path: Path) -> None:
    passages = [make_passage(CLEAN_PROSE)]

    accepted = apply_quality_gate(
        passages, source=make_source(), path=tmp_path / "report.pdf"
    )

    assert accepted == passages


# Long tokens that are not failures


def test_a_long_url_does_not_fail_a_passage() -> None:
    """Regression, from a sources line in a real ASIL report. The check is for
    text that has lost its spaces, which a URL has not."""
    url = (
        "https://www.aph.gov.au/Parliamentary_Business/Committees/Senate/"
        "Adopting_Artificial_Intelligence_AI/AdoptingAI/Report/"
        "Chapter_4_-_Impacts_of_AI_on_industry_business_and_workers"
    )
    assert len(url) > MAX_TOKEN_LENGTH
    passage = make_passage(f"Sources: FYA New Basics (2016), AlphaBeta. See {url}")

    assert check_passage(passage) is None


def test_a_long_email_address_does_not_fail_a_passage() -> None:
    passage = make_passage(
        "Enquiries about this publication should be directed to "
        "enterprise.skills.research.team.2026@a-start-in-life.example.org today."
    )

    assert check_passage(passage) is None


def test_a_smeared_paragraph_still_fails() -> None:
    passage = make_passage("Enterpriseskillsdemandroseacrosseveryindustrydivision" * 2)

    failure = check_passage(passage)

    assert failure is not None
    assert failure.issue is QualityIssue.OVERLONG_TOKEN


def test_a_url_does_not_mask_a_smeared_token_beside_it() -> None:
    """Exempting URLs must not exempt the rest of the passage."""
    smear = "x" * (MAX_TOKEN_LENGTH + 10)
    passage = make_passage(f"See https://example.org/a/very/long/path/indeed {smear}")

    failure = check_passage(passage)

    assert failure is not None
    assert failure.issue is QualityIssue.OVERLONG_TOKEN
